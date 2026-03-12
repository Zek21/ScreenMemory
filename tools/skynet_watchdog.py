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
CONSULTANT_BRIDGES = (
    {
        "service_name": "consultant_bridge",
        "label": "Codex Consultant bridge",
        "consultant_id": "consultant",
        "api_port": 8422,
        "pid_file": DATA_DIR / "consultant_bridge.pid",
        "extra_args": [],
    },
    {
        "service_name": "gemini_consultant_bridge",
        "label": "Gemini Consultant bridge",
        "consultant_id": "gemini_consultant",
        "api_port": 8425,
        "pid_file": DATA_DIR / "gemini_consultant_bridge.pid",
        "extra_args": [
            "--id", "gemini_consultant",
            "--display-name", "Gemini Consultant",
            "--model", "Gemini 3 Pro",
            "--source", "GC-Start",
            "--api-port", "8425",
        ],
    },
)

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

def _load_watchdog_config():
    """Load watchdog intervals from brain_config.json, with sane defaults."""
    cfg_path = ROOT / "data" / "brain_config.json"
    defaults = {"watchdog_interval": 30, "god_check_interval": 30, "skynet_check_interval": 60}
    try:
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            wd = data.get("watchdog", {})
            return {k: wd.get(k, defaults[k]) for k in defaults}
    except Exception:
        pass
    return defaults

_WD_CFG = _load_watchdog_config()
WATCHDOG_INTERVAL = _WD_CFG["watchdog_interval"]
GOD_CHECK_INTERVAL = _WD_CFG["god_check_interval"]
SKYNET_CHECK_INTERVAL = _WD_CFG["skynet_check_interval"]
AWARENESS_INTERVAL = 60
WINDOW_SCAN_INTERVAL = 30
STUCK_CHECK_INTERVAL = 30
GUARD_REFRESH_INTERVAL = 60
SSE_CHECK_INTERVAL = 30
HWND_CHECK_INTERVAL = 30
LEARNER_CHECK_INTERVAL = 60
CONSULTANT_CHECK_INTERVAL = 30

# ── Restart backoff tracking ─────────────────────────────────────────────────
# Prevents restart storms when a service is persistently broken.
# After MAX_RESTART_ATTEMPTS consecutive failures, enters cooldown.
MAX_RESTART_ATTEMPTS = 3
RESTART_COOLDOWN_S = 600  # 10 minutes before retrying after max attempts

_restart_state: dict[str, dict] = {}  # service → {attempts, last_failure, cooldown_until}


def _should_attempt_restart(service_name: str) -> bool:
    """Check if a restart should be attempted (respects backoff limits).

    Returns True if restart is allowed, False if in cooldown.
    Resets attempt counter when cooldown expires.
    """
    state = _restart_state.get(service_name)
    if not state:
        return True
    now = time.time()
    # Cooldown expired — reset
    if now >= state.get("cooldown_until", 0):
        if state.get("attempts", 0) >= MAX_RESTART_ATTEMPTS:
            _restart_state[service_name] = {"attempts": 0, "last_failure": 0, "cooldown_until": 0}
        return True
    # Still in cooldown
    remaining = int(state["cooldown_until"] - now)
    log(f"{service_name}: restart cooldown active ({remaining}s remaining, {state['attempts']} consecutive failures)")
    return False


def _record_restart_result(service_name: str, success: bool):
    """Record whether a restart succeeded or failed for backoff tracking."""
    if service_name not in _restart_state:
        _restart_state[service_name] = {"attempts": 0, "last_failure": 0, "cooldown_until": 0}
    state = _restart_state[service_name]
    if success:
        state["attempts"] = 0
        state["cooldown_until"] = 0
    else:
        state["attempts"] = state.get("attempts", 0) + 1
        state["last_failure"] = time.time()
        if state["attempts"] >= MAX_RESTART_ATTEMPTS:
            state["cooldown_until"] = time.time() + RESTART_COOLDOWN_S
            log(f"RESTART BACKOFF: {service_name} failed {state['attempts']} times -- "
                f"cooling down for {RESTART_COOLDOWN_S}s. Manual intervention may be required.")
            _post_bus_alert_safe(
                f"RESTART_BACKOFF: {service_name} failed {state['attempts']} consecutive restarts. "
                f"Cooling down {RESTART_COOLDOWN_S}s. Manual intervention may be required."
            )


def _post_bus_alert_safe(content: str):
    """Best-effort bus alert (used during restart tracking). Uses SpamGuard with raw fallback."""
    try:
        from tools.skynet_spam_guard import guarded_publish
        guarded_publish({
            "sender": "watchdog", "topic": "orchestrator",
            "type": "alert", "content": content
        })
    except Exception:
        # SpamGuard unavailable (import error, Skynet down) -- raw fallback
        try:
            data = json.dumps({
                "sender": "watchdog", "topic": "orchestrator",
                "type": "alert", "content": content
            }).encode()
            req = urllib.request.Request(
                "http://localhost:8420/bus/publish", data=data,
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass  # signed: alpha


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


MAX_LOG_SIZE = 1_000_000  # 1MB -- rotate log file to prevent unbounded growth

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    DATA_DIR.mkdir(exist_ok=True)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_SIZE:
            # Keep last 500KB
            content = LOG_FILE.read_text(encoding="utf-8", errors="replace")
            LOG_FILE.write_text(content[-500_000:], encoding="utf-8")
    except Exception:
        pass
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def check_url(url: str, timeout: int = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _pid_alive(pid):
    try:
        pid_i = int(pid)
    except Exception:
        return False
    if pid_i <= 0:
        return False
    if psutil is not None:
        try:
            proc = psutil.Process(pid_i)
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False
    if sys.platform == "win32":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid_i)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
        except Exception:
            return False
        return False
    try:
        import os
        os.kill(pid_i, 0)
        return True
    except Exception:
        return False


def _read_pid_file(pid_file: Path) -> int:
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _pid_alive_and_correct(pid: int, expected_script: str) -> bool:  # signed: beta
    """Check if PID is alive AND running the expected Python script.

    Prevents PID reuse attacks where OS reassigns a dead daemon's PID
    to an unrelated process (e.g. svchost.exe). Without this check,
    the watchdog would skip restart, leaving the real daemon dead.
    """
    if pid <= 0:
        return False
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return False
            cmdline = " ".join(proc.cmdline()).lower()
            return expected_script.lower() in cmdline
        except Exception:
            return False
    # Fallback: can only check alive, not script name
    return _pid_alive(pid)  # signed: beta


def _consultant_endpoint_payload(api_port: int, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(f"http://localhost:{api_port}/consultants", timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _consultant_bridge_is_healthy(config: dict) -> bool:
    pid_file = config["pid_file"]
    pid = _read_pid_file(pid_file) if pid_file.exists() else 0
    if pid and not _pid_alive(pid):
        return False
    payload = _consultant_endpoint_payload(config["api_port"], timeout=3.0)
    if not isinstance(payload, dict):
        return False
    consultant = payload.get("consultant")
    if not isinstance(consultant, dict):
        return False
    if str(consultant.get("id") or "").lower() != str(config["consultant_id"]).lower():
        return False
    return bool(consultant.get("live")) or str(consultant.get("status") or "").upper() == "LIVE"


def restart_god_console():
    """Restart god_console.py as a hidden background process (with backoff)."""
    if not _should_attempt_restart("god_console"):
        return False
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
            _record_restart_result("god_console", True)
            return True
        else:
            log("GOD Console restart FAILED -- still not responding")
            _record_restart_result("god_console", False)
            return False
    except Exception as e:
        log(f"GOD Console restart error: {e}")
        _record_restart_result("god_console", False)
        return False


def restart_skynet():
    """Restart skynet.exe as a hidden background process (with backoff)."""
    if not _should_attempt_restart("skynet"):
        return False
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
            _record_restart_result("skynet", True)
            return True
        else:
            log("Skynet backend restart FAILED -- still not responding")
            _record_restart_result("skynet", False)
            return False
    except Exception as e:
        log(f"Skynet backend restart error: {e}")
        _record_restart_result("skynet", False)
        return False


def restart_sse_daemon():
    """Restart skynet_sse_daemon.py as a hidden background process (with backoff)."""
    # ── PID guard: check if SSE daemon is already running via its PID file ──
    sse_pid_file = DATA_DIR / "sse_daemon.pid"
    if sse_pid_file.exists():
        try:
            old_pid_val = int(sse_pid_file.read_text().strip())
            if _pid_alive_and_correct(old_pid_val, "skynet_sse_daemon"):  # signed: beta
                log(f"SSE daemon already running (PID {old_pid_val}) -- skipping restart")
                _record_restart_result("sse_daemon", True)
                return True
        except (OSError, ValueError):
            pass  # Stale PID file -- proceed with restart

    if not _should_attempt_restart("sse_daemon"):
        return False

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
                    _record_restart_result("sse_daemon", True)
                    return True
            except Exception:
                pass
        log("SSE daemon restart -- cannot verify (may be starting)")
        _record_restart_result("sse_daemon", True)  # optimistic
        return True  # optimistic; next check cycle will verify
    except Exception as e:
        log(f"SSE daemon restart error: {e}")
        _record_restart_result("sse_daemon", False)
        return False


def restart_learner():
    """Restart skynet_learner.py as a hidden background daemon (with backoff)."""
    learner_pid_file = DATA_DIR / "learner.pid"
    if learner_pid_file.exists():
        try:
            old_pid_val = int(learner_pid_file.read_text().strip())
            if _pid_alive_and_correct(old_pid_val, "skynet_learner"):  # signed: beta
                log(f"Learner daemon already running (PID {old_pid_val}) -- skipping restart")
                _record_restart_result("learner", True)
                return True
        except (OSError, ValueError):
            pass  # Stale PID file -- proceed with restart

    if not _should_attempt_restart("learner"):
        return False

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
                _record_restart_result("learner", True)
                return True
            except (OSError, ValueError):
                pass
        log("Learner daemon restart -- cannot verify (may be starting)")
        _record_restart_result("learner", True)  # optimistic
        return True
    except Exception as e:
        log(f"Learner daemon restart error: {e}")
        _record_restart_result("learner", False)
        return False


def restart_consultant_bridge(config: dict):
    """Restart a consultant bridge and verify its live /consultants surface (with backoff)."""
    svc_name = config["service_name"]
    pid_file = config["pid_file"]
    old_pid = _read_pid_file(pid_file) if pid_file.exists() else 0
    if old_pid and _pid_alive(old_pid):
        if _consultant_bridge_is_healthy(config):
            log(f"{config['label']} already running (PID {old_pid}) -- skipping restart")
            _record_restart_result(svc_name, True)
            return True

    if not _should_attempt_restart(svc_name):
        return False

    log(f"{config['label']} DOWN -- attempting restart")
    try:
        cmd = [PYTHON, str(ROOT / "tools" / "skynet_consultant_bridge.py"), *config.get("extra_args", [])]
        subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=_DAEMON_ENV,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )
        time.sleep(3)
        if _consultant_bridge_is_healthy(config):
            new_pid = _read_pid_file(pid_file) if pid_file.exists() else _port_to_pid(config["api_port"])
            log(f"{config['label']} restarted (old={old_pid}, new={new_pid})")
            _post_restart_alert(f"{svc_name}.py", old_pid, new_pid)
            _refresh_protected_registry()
            _log_incident(f"{svc_name}_restart", old_pid, new_pid)
            _record_restart_result(svc_name, True)
            return True
        log(f"{config['label']} restart FAILED -- bridge still not live")
        _record_restart_result(svc_name, False)
        return False
    except Exception as e:
        log(f"{config['label']} restart error: {e}")
        _record_restart_result(svc_name, False)
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
    """Post a SERVICE_RESTARTED alert to the bus via SpamGuard."""
    content = f"SERVICE_RESTARTED: {service_name}, old_pid={old_pid}, new_pid={new_pid}"
    try:
        from tools.skynet_spam_guard import guarded_publish
        guarded_publish({
            "sender": "watchdog", "topic": "orchestrator",
            "type": "alert", "content": content
        })
    except Exception:
        try:
            data = json.dumps({
                "sender": "watchdog", "topic": "orchestrator",
                "type": "alert", "content": content
            }).encode()
            req = urllib.request.Request(
                "http://localhost:8420/bus/publish", data=data,
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            log(f"Failed to post restart alert for {service_name}")
    # signed: alpha


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
        try:
            from tools.skynet_atomic import atomic_update_json
        except ModuleNotFoundError:
            from skynet_atomic import atomic_update_json
        incidents_file = DATA_DIR / "incidents.json"
        def _append_incident(incidents):
            if not isinstance(incidents, list):
                incidents = []
            incidents.append({
                "id": incident_id,
                "timestamp": datetime.now().isoformat(),
                "old_pid": old_pid,
                "new_pid": new_pid,
                "type": "auto_restart",
            })
            return incidents[-100:]
        atomic_update_json(incidents_file, _append_incident, default=[])
    except Exception:
        pass


def _check_worker_hwnds_internal():
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
_heartbeat_cache = {}  # in-memory cache to avoid constant file reads

def _update_heartbeat(service_name, is_alive):
    """Update the heartbeat timestamp for a service (atomic write)."""
    try:
        try:
            from tools.skynet_atomic import atomic_write_json
        except ModuleNotFoundError:
            from skynet_atomic import atomic_write_json
        if is_alive:
            _heartbeat_cache[service_name] = {
                "last_seen": time.time(),
                "last_seen_ts": datetime.now().isoformat(),
                "status": "alive",
            }
        else:
            entry = _heartbeat_cache.get(service_name, {})
            entry["status"] = "dead"
            _heartbeat_cache[service_name] = entry
        atomic_write_json(HEARTBEAT_FILE, _heartbeat_cache)
    except Exception:
        pass


def _is_service_stale(service_name):
    """Check if a service heartbeat is stale (>HEARTBEAT_TIMEOUT seconds old)."""
    try:
        entry = _heartbeat_cache.get(service_name, {})
        last_seen = entry.get("last_seen", 0)
        if last_seen and (time.time() - last_seen) > HEARTBEAT_TIMEOUT:
            return True
    except Exception:
        pass
    return False


def write_status(status: dict):
    try:
        from tools.skynet_atomic import atomic_write_json
    except ModuleNotFoundError:
        from skynet_atomic import atomic_write_json
    DATA_DIR.mkdir(exist_ok=True)
    status["updated"] = datetime.now().isoformat()
    atomic_write_json(STATUS_FILE, status)


def _check_god_console(now, last_check, status):
    """Check GOD Console health and auto-restart if down."""
    if now - last_check < GOD_CHECK_INTERVAL:
        return last_check
    god_ok = check_url(f"{GOD_CONSOLE_URL}/health")
    _update_heartbeat("god_console", god_ok)
    if god_ok:
        status["god_console"] = "ok"
    else:
        restarted = restart_god_console()
        status["god_console"] = "restarted" if restarted else "down"
    status["god_last_check"] = datetime.now().isoformat()
    return now


def _check_skynet_backend(now, last_check, status):
    """Check Skynet backend health and auto-restart if down."""
    if now - last_check < SKYNET_CHECK_INTERVAL:
        return last_check
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
    return now


def _check_sse_daemon(now, last_check, status):
    """Check SSE daemon health via PID file and realtime.json freshness."""
    if now - last_check < SSE_CHECK_INTERVAL:
        return last_check
    sse_ok = False
    sse_pid_file = DATA_DIR / "sse_daemon.pid"
    if sse_pid_file.exists():
        try:
            sse_pid_val = int(sse_pid_file.read_text().strip())
            import os
            os.kill(sse_pid_val, 0)
            sse_ok = True
        except (OSError, ValueError):
            pass
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
    return now


def _check_learner_daemon(now, last_check, status):
    """Check learner daemon health via PID file."""
    if now - last_check < LEARNER_CHECK_INTERVAL:
        return last_check
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
    return now


def _check_consultant_bridges(now, last_check, status):
    """Check all consultant bridge health endpoints."""
    if now - last_check < CONSULTANT_CHECK_INTERVAL:
        return last_check
    for config in CONSULTANT_BRIDGES:
        bridge_ok = _consultant_bridge_is_healthy(config)
        _update_heartbeat(config["service_name"], bridge_ok)
        if bridge_ok:
            status[config["service_name"]] = "ok"
        else:
            restarted = restart_consultant_bridge(config)
            status[config["service_name"]] = "restarted" if restarted else "down"
    status["consultant_last_check"] = datetime.now().isoformat()
    return now


def _check_worker_hwnds(now, last_check, status):
    """Check worker window HWNDs are still valid."""
    if now - last_check < HWND_CHECK_INTERVAL:
        return last_check
    try:
        dead_workers = _check_worker_hwnds_internal()
        status["dead_worker_windows"] = len(dead_workers)
        status["hwnd_last_check"] = datetime.now().isoformat()
        if dead_workers:
            names = [d["name"] for d in dead_workers]
            log(f"CRITICAL: Worker window(s) GONE: {names}")
            _post_bus_alert(f"WORKER_WINDOW_DEAD: {', '.join(names)} -- HWNDs invalid")  # signed: alpha
    except Exception as e:
        log(f"Worker HWND check failed: {e}")
    return now


def _run_window_scan(now, last_check, status):
    """Periodic window scan for dead windows."""
    if now - last_check < WINDOW_SCAN_INTERVAL:
        return last_check
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
    return now


def _run_stuck_detection(now, last_check, status, stuck_detector):
    """Periodic stuck worker detection."""
    if now - last_check < STUCK_CHECK_INTERVAL:
        return last_check, stuck_detector
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
    return now, stuck_detector


def _run_awareness_broadcast(now, last_check, status, skynet_self_cached):
    """Periodic self-awareness broadcast with IQ scoring."""
    if now - last_check < AWARENESS_INTERVAL:
        return last_check, skynet_self_cached
    try:
        from tools.skynet_self import SkynetSelf
        if skynet_self_cached is None:
            skynet_self_cached = SkynetSelf()
        pulse = skynet_self_cached.broadcast_awareness()
        status["last_awareness"] = datetime.now().isoformat()
        status["iq"] = pulse.get("iq", 0)
        log(f"Awareness broadcast OK (IQ={pulse.get('iq', 0):.3f})")
    except Exception as e:
        log(f"Awareness broadcast failed: {e} (ROOT={ROOT}, sys.path[0]={sys.path[0]})")
    return now, skynet_self_cached


def _run_guard_refresh(now, last_check, status):
    """Periodic process guard registry refresh and duplicate detection."""
    if now - last_check < GUARD_REFRESH_INTERVAL:
        return last_check
    try:
        from tools.skynet_process_guard import refresh_registry
        reg = refresh_registry()
        status["protected_processes"] = reg.get("process_count", 0)
        status["guard_refresh"] = datetime.now().isoformat()
        from collections import Counter
        role_counts = Counter(p["role"] for p in reg.get("processes", [])
                              if p["role"] not in ("worker", "orchestrator"))
        dups = {r: c for r, c in role_counts.items() if c > 1}
        if dups:
            dup_str = ", ".join(f"{r}={c}" for r, c in dups.items())
            log(f"DUPLICATE ALERT: {dup_str}")
            status["duplicate_services"] = dups
            _post_bus_alert(f"DUPLICATE_SERVICES: {dup_str} -- orchestrator should clean up")
    except Exception as e:
        log(f"Process guard refresh failed: {e}")
    return now


def _check_dispatch_timeouts(now, last_check, status):
    """Check for dispatches that never received a result."""
    if now - last_check < SKYNET_CHECK_INTERVAL:
        return last_check
    try:
        dispatch_log = DATA_DIR / "dispatch_log.json"
        if dispatch_log.exists():
            all_entries = json.loads(dispatch_log.read_text(encoding="utf-8"))
            entries = all_entries[-100:] if len(all_entries) > 100 else all_entries
            if len(all_entries) > 500:
                try:
                    dispatch_log.write_text(json.dumps(all_entries[-200:], indent=2), encoding="utf-8")
                except Exception:
                    pass
            del all_entries
            stale = []
            for e in entries:
                if e.get("success") and not e.get("result_received"):
                    ts = datetime.fromisoformat(e["timestamp"])
                    age_s = (datetime.now() - ts).total_seconds()
                    if age_s > 300:
                        stale.append({"worker": e["worker"], "task": e["task_summary"][:60], "age_min": round(age_s / 60, 1)})
            if stale:
                status["stale_dispatches"] = len(stale)
                summary = ", ".join(f"{s['worker']}({s['age_min']}m)" for s in stale[:5])
                log(f"TIMEOUT ALERT: {len(stale)} dispatch(es) without result >5min: {summary}")
                _post_bus_alert(f"DISPATCH_TIMEOUT: {len(stale)} workers silent >5min: {summary}")
            else:
                status.pop("stale_dispatches", None)
    except Exception as e:
        log(f"Dispatch timeout check failed: {e}")
    return now


def _post_bus_alert(content: str):
    """Post an alert message to the Skynet bus via SpamGuard."""
    try:
        from tools.skynet_spam_guard import guarded_publish
        guarded_publish({
            "sender": "watchdog", "topic": "orchestrator",
            "type": "alert", "content": content
        })
    except Exception:
        try:
            data = json.dumps({
                "sender": "watchdog", "topic": "orchestrator",
                "type": "alert", "content": content
            }).encode()
            req = urllib.request.Request(
                "http://localhost:8420/bus/publish", data=data,
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass  # signed: alpha


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

    # ── atexit PID cleanup (safety net for abnormal exits) ──  # signed: beta
    import atexit
    def _cleanup_pid():
        try:
            if PID_FILE.exists() and int(PID_FILE.read_text().strip()) == os.getpid():
                PID_FILE.unlink()
        except Exception:
            pass
    atexit.register(_cleanup_pid)  # signed: beta

    # ── SIGTERM handler for graceful shutdown ──  # signed: alpha
    import signal
    _wd_shutdown = False
    def _wd_sigterm_handler(signum, frame):
        nonlocal _wd_shutdown
        _wd_shutdown = True
        log(f"Received signal {signum} -- requesting graceful shutdown")
    signal.signal(signal.SIGTERM, _wd_sigterm_handler)
    try:
        signal.signal(signal.SIGBREAK, _wd_sigterm_handler)  # Windows Ctrl+Break
    except (AttributeError, OSError):
        pass  # SIGBREAK only on Windows  # signed: alpha

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
    last_consultant_check = 0.0
    last_dispatch_check = 0.0
    stuck_detector = None
    skynet_self_cached = None
    status = {
        "god_console": "unknown",
        "skynet": "unknown",
        "sse_daemon": "unknown",
        "learner": "unknown",
    }
    for config in CONSULTANT_BRIDGES:
        status[config["service_name"]] = "unknown"

    _consecutive_loop_errors = 0  # signed: gamma
    DEGRADED_THRESHOLD = 10  # signed: gamma

    try:
        while True:
            if _wd_shutdown:  # signed: alpha
                log("SIGTERM/SIGBREAK received -- shutting down gracefully")
                break
            now = time.time()
            cycle += 1

            if max_runtime and (now - start_time) >= max_runtime:
                log(f"Max runtime {max_runtime}s reached -- shutting down gracefully")
                break

            try:
                last_god_check = _check_god_console(now, last_god_check, status)
                last_skynet_check = _check_skynet_backend(now, last_skynet_check, status)
                last_sse_check = _check_sse_daemon(now, last_sse_check, status)
                last_learner_check = _check_learner_daemon(now, last_learner_check, status)
                last_consultant_check = _check_consultant_bridges(now, last_consultant_check, status)
                last_hwnd_check = _check_worker_hwnds(now, last_hwnd_check, status)
                last_window_scan = _run_window_scan(now, last_window_scan, status)
                last_stuck_check, stuck_detector = _run_stuck_detection(now, last_stuck_check, status, stuck_detector)
                last_awareness, skynet_self_cached = _run_awareness_broadcast(now, last_awareness, status, skynet_self_cached)
                last_guard_refresh = _run_guard_refresh(now, last_guard_refresh, status)
                last_dispatch_check = _check_dispatch_timeouts(now, last_dispatch_check, status)
                _consecutive_loop_errors = 0  # reset on successful cycle  # signed: gamma
            except (ConnectionError, TimeoutError, OSError) as e:
                _consecutive_loop_errors += 1
                log(f"Watchdog cycle network error ({_consecutive_loop_errors}): {e}")
            except (json.JSONDecodeError, FileNotFoundError, ValueError) as e:
                _consecutive_loop_errors += 1
                log(f"Watchdog cycle data error ({_consecutive_loop_errors}): {e}")
            except Exception as e:
                _consecutive_loop_errors += 1
                log(f"Watchdog cycle error ({_consecutive_loop_errors}): {e}")
            if _consecutive_loop_errors >= DEGRADED_THRESHOLD and _consecutive_loop_errors % DEGRADED_THRESHOLD == 0:
                _post_bus_alert_safe(f"DAEMON_DEGRADED: skynet_watchdog hit {_consecutive_loop_errors} consecutive errors")  # signed: gamma

            write_status(status)
            time.sleep(WATCHDOG_INTERVAL)
    except KeyboardInterrupt:
        log("Watchdog shutting down (Ctrl+C)")
        _post_bus_alert_safe(f"Watchdog shutdown: KeyboardInterrupt after {cycle} cycles")  # signed: alpha
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
