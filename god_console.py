#!/usr/bin/env python3
"""
GOD Console v3 Level 3 — Futuristic Web-Based Command Center for SKYNET.
Launches a local HTTP server and opens the dashboard in your browser.
Connects to Skynet backend at localhost:8420 via SSE for real-time streaming.

Usage:
    python god_console.py              # default port 8421
    python god_console.py --port 9000  # custom port
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time as _time
import threading
import traceback
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path

try:
    import psutil
except Exception:
    psutil = None

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
PID_FILE = DATA_DIR / "god_console.pid"
sys.path.insert(0, str(ROOT / "tools"))

logger = logging.getLogger("skynet.god_console")

# Try fast JSON (orjson) with stdlib fallback
try:
    import orjson as _json_fast
    def _dumps(obj): return _json_fast.dumps(obj, default=str).decode()
except ImportError:
    def _dumps(obj): return json.dumps(obj, default=str)

DASHBOARD_HTML = ROOT / "dashboard.html"
DEFAULT_PORT = 8421
WS_PORT = 8423
ACCESS_LOG = ROOT / "data" / "console_access.log"
TODOS_FILE = ROOT / "data" / "todos.json"
_SERVER_START = _time.time()


def _fetch_backend(url, timeout=2, retries=1):
    """Fetch from Skynet backend with retry logic."""
    from urllib.request import urlopen
    from urllib.error import URLError
    last_err = None
    for i in range(retries + 1):
        try:
            return json.loads(urlopen(url, timeout=timeout).read())
        except (URLError, OSError, json.JSONDecodeError) as e:
            last_err = e
            if i < retries:
                _time.sleep(0.2 * (i + 1))
    raise last_err


# --------------- Aggressive Cache Layer ---------------
_cache = {
    "pulse": None, "pulse_t": 0,
    "status": None, "status_t": 0,
    "introspect": None, "introspect_t": 0,
    "assess": None, "assess_t": 0,
    "backend_status": None, "backend_status_t": 0,
    "consultants": None, "consultants_t": 0,
    "bus": None, "bus_t": 0, "bus_limit": 20,
    "engines": None, "engines_t": 0,
    "dashboard_data": None, "dashboard_data_t": 0,
    "windows": None, "windows_t": 0,
}
_PULSE_TTL = 15      # pulse is expensive; 15s cache avoids stampede
_STATUS_TTL = 10
_INTROSPECT_TTL = 30
_BACKEND_TTL = 3     # backend status cached 3s
_CONSULTANT_TTL = 2  # consultant bridge state cached 2s
_CONSULTANT_PORTS = (8422, 8424, 8425)
_BUS_TTL = 2         # bus messages cached 2s
_ENGINES_TTL = 30    # engine probes are expensive; 30s cache
_DASHBOARD_TTL = 3   # combined dashboard data
_WINDOWS_TTL = 5     # window scan cached 5s

# Learner endpoint caches
_LEARNER_HEALTH_TTL = 3   # health cached 3s
_LEARNER_METRICS_TTL = 5  # metrics cached 5s (sparkline is expensive)
_ACTIVITY_TTL = 2         # worker activity cached 2s (changes fast)
_learner_cache = {
    "health": None, "health_t": 0, "health_hits": 0, "health_misses": 0,
    "metrics": None, "metrics_t": 0, "metrics_hits": 0, "metrics_misses": 0,
}
_activity_cache = {}      # in-memory store for POST /api/worker/{name}/activity
_activity_file_cache = None
_activity_file_cache_t = 0

_cache_lock = threading.Lock()
_pulse_compute_lock = threading.Lock()   # prevents stampeding herd on pulse
_skynet_self_instance = None


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if psutil is not None:
        try:
            return psutil.pid_exists(pid)
        except Exception:
            pass
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


def _claim_pid(label: str) -> bool:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(str(PID_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                old_pid = int(PID_FILE.read_text().strip())
            except Exception:
                old_pid = 0
            if _pid_alive(old_pid):
                print(f"[{label}] Already running (PID {old_pid}) -- exiting to prevent duplicate")
                return False
            try:
                PID_FILE.unlink()
            except FileNotFoundError:
                continue
            except Exception:
                print(f"[{label}] Stale PID file could not be cleared: {PID_FILE}")
                return False
            continue
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(str(os.getpid()))
            return True
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            try:
                if PID_FILE.exists():
                    PID_FILE.unlink()
            except Exception:
                pass
            raise


def _cleanup_pid() -> None:
    try:
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()
    except Exception:
        pass


def _get_skynet_self():
    global _skynet_self_instance
    if _skynet_self_instance is None:
        from skynet_self import SkynetSelf
        _skynet_self_instance = SkynetSelf()
    return _skynet_self_instance

def _cached_pulse():
    now = _time.time()
    with _cache_lock:
        if _cache["pulse"] and (now - _cache["pulse_t"]) < _PULSE_TTL:
            return _cache["pulse"]
    with _pulse_compute_lock:
        with _cache_lock:
            if _cache["pulse"] and (_time.time() - _cache["pulse_t"]) < _PULSE_TTL:
                return _cache["pulse"]
        data = _build_pulse_data()
        with _cache_lock:
            _cache["pulse"] = data
            _cache["pulse_t"] = _time.time()
        return data


def _build_pulse_data():
    """Compute the full pulse payload from skynet self and engines."""
    skynet = _get_skynet_self()
    raw_pulse = skynet.quick_pulse()
    engines_online, engines_total, engine_names = _get_engine_counts()
    agents = raw_pulse.get("agents", {})
    alive = raw_pulse.get("alive", 0)
    total = raw_pulse.get("total", 5)
    workers = {aid: {"status": status, "model": "opus-fast"} for aid, status in agents.items()}
    iq = _compute_display_iq(raw_pulse)
    iq_breakdown = _compute_iq_breakdown(alive, total, engines_online, engines_total)
    assessment = (
        f"{alive}/{total} workers connected. "
        f"{engines_online}/{engines_total} engines online: {', '.join(engine_names[:6])}. "
        f"Health: {raw_pulse.get('health','UNKNOWN')}. Level 3 intelligence active."
    )
    return {
        "identity": "SKYNET v3.0 -- Distributed Intelligence Network (Level 3)",
        "intelligence_score": iq,
        "iq_breakdown": iq_breakdown,
        "engines_online": engines_online,
        "engines_total": engines_total,
        "health": raw_pulse.get("health", "UNKNOWN"),
        "self_assessment": assessment,
        "convene_sessions": [],
        "workers": workers,
        "aware": alive > 0,
        "name": raw_pulse.get("name", "SKYNET"),
        "ts": raw_pulse.get("ts"),
        "alive": alive,
        "total": total,
        "iq_trend": raw_pulse.get("iq_trend", "stable"),
    }


def _get_engine_counts():
    """Return (online_count, total_count, online_names) from cached engines."""
    try:
        engines_data = _cached_engines()
        engines = engines_data.get("engines", {})
        online = sum(1 for e in engines.values() if e.get("status") == "online")
        names = [e.get("name", k) for k, e in engines.items() if e.get("status") == "online"]
        return online, len(engines), names
    except Exception:
        return 0, 0, []


def _compute_display_iq(raw_pulse):
    """Normalize IQ from raw pulse (0-1 scale or 0-100 scale) to display value."""
    iq_raw = raw_pulse.get("iq", 0)
    return round(iq_raw * 100) if iq_raw <= 1 else round(iq_raw)


def _compute_iq_breakdown(alive, total, engines_online, engines_total):
    """Build lightweight IQ breakdown from available counts."""
    worker_pct = alive / max(1, total)
    engine_pct = engines_online / max(1, engines_total)
    return {
        "workers": {"score": round(worker_pct * 25, 1), "detail": f"{alive}/{total} alive"},
        "engines": {"score": round(engine_pct * 25, 1), "detail": f"{engines_online}/{engines_total} online"},
        "uptime": {"score": min(10, round((_time.time() - _SERVER_START) / 3600 * 2.5, 1)), "detail": f"{(_time.time() - _SERVER_START)/60:.0f}min"},
        "bus": {"score": 10, "detail": "connected"},
    }

def _cached_status():
    now = _time.time()
    with _cache_lock:
        if _cache["status"] and (now - _cache["status_t"]) < _STATUS_TTL:
            return _cache["status"]
    data = _get_skynet_self().full_status()
    with _cache_lock:
        _cache["status"] = data
        _cache["status_t"] = _time.time()
    return data

def _cached_consultants():
    """Cached live consultant bridge state."""
    now = _time.time()
    with _cache_lock:
        if _cache["consultants"] and (now - _cache["consultants_t"]) < _CONSULTANT_TTL:
            return _cache["consultants"]
    try:
        data = _probe_all_consultant_bridges()
        if not data:
            data = _fallback_consultant_view()
    except Exception as e:
        data = {"error": str(e)}
    with _cache_lock:
        _cache["consultants"] = data
        _cache["consultants_t"] = _time.time()
    return data


def _probe_all_consultant_bridges():
    """Probe all consultant bridge ports in parallel threads. Returns dict of live consultants."""
    data = {}
    data_lock = threading.Lock()

    def _probe(port):
        try:
            payload = _fetch_backend(f"http://localhost:{port}/consultants", timeout=0.8, retries=0)
        except Exception:
            return
        consultant = payload.get("consultant") if isinstance(payload, dict) else None
        if not isinstance(consultant, dict):
            return
        cid = str(consultant.get("id") or f"consultant_{port}")
        _enrich_consultant_liveness(consultant)
        with data_lock:
            existing = data.get(cid)
            if existing is None or (consultant.get("live") and not existing.get("live")):
                data[cid] = consultant

    threads = [threading.Thread(target=_probe, args=(port,), daemon=True) for port in _CONSULTANT_PORTS]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1.2)
    return data


def _enrich_consultant_liveness(consultant):
    """Set live/status/pid_alive fields based on heartbeat age."""
    age = consultant.get("heartbeat_age_s")
    stale_after = consultant.get("stale_after_s", 8)
    try:
        age_f, stale_f = float(age), float(stale_after)
    except Exception:
        return
    if age_f <= stale_f:
        consultant["live"] = True
        consultant["status"] = "LIVE"
        consultant["pid_alive"] = True


def _fallback_consultant_view():
    """Fallback: get consultant view from local bridge module."""
    from skynet_consultant_bridge import get_consultant_view
    consultant = get_consultant_view()
    if consultant:
        return {str(consultant.get("id") or "consultant"): consultant}
    return {}

def _cached_backend_status():
    """Cached backend /status with per-worker enrichment."""
    now = _time.time()
    with _cache_lock:
        if _cache["backend_status"] and (now - _cache["backend_status_t"]) < _BACKEND_TTL:
            return _cache["backend_status"]
    backend = _fetch_backend("http://localhost:8420/status", timeout=2)
    # Enrich per-worker data
    agents = backend.get("agents", {})
    bus_msgs = backend.get("bus", [])
    # Read true UI states from worker_health.json
    try:
        if os.path.exists("data/worker_health.json"):
            with open("data/worker_health.json", "r") as f:
                import json as json
                hw = json.load(f)
        else:
            hw = {}
    except Exception:
        hw = {}
        
    for wname, wdata in agents.items():
        if wname in hw and "status" in hw[wname]:
            wdata["status"] = hw[wname]["status"]
        if "agent" in hw.get(wname, {}):
            wdata["agent"] = hw[wname]["agent"]
        if "model" in hw.get(wname, {}):
            wdata["model"] = hw[wname]["model"]
            
        task = wdata.get("current_task", "")
        wdata["current_task_short"] = task[:80] + ("..." if len(task) > 80 else "") if task else ""
        # Count missions from bus
        mission_count = sum(1 for m in bus_msgs if m.get("sender") == wname and m.get("type") == "result")
        wdata["mission_count"] = mission_count
        hb = wdata.get("last_heartbeat", "")
        wdata["last_active"] = hb if hb else wdata.get("recent_logs", [""])[0][:20]
    backend["consultants"] = _cached_consultants()
    with _cache_lock:
        _cache["backend_status"] = backend
        _cache["backend_status_t"] = _time.time()
    return backend

def _cached_bus(limit=20):
    """Cached bus messages."""
    now = _time.time()
    with _cache_lock:
        if _cache["bus"] and (now - _cache["bus_t"]) < _BUS_TTL and _cache["bus_limit"] == limit:
            return _cache["bus"]
    data = _fetch_backend(f"http://localhost:8420/bus/messages?limit={limit}", timeout=2)
    with _cache_lock:
        _cache["bus"] = data
        _cache["bus_t"] = _time.time()
        _cache["bus_limit"] = limit
    return data

def _cached_engines():
    """Cached engine metrics with response_time_ms per engine."""
    now = _time.time()
    with _cache_lock:
        if _cache["engines"] and (now - _cache["engines_t"]) < _ENGINES_TTL:
            return _cache["engines"]
    from engine_metrics import collect_engine_metrics
    t0 = _time.time()
    data = collect_engine_metrics()
    total_ms = (_time.time() - t0) * 1000
    data["total_probe_ms"] = round(total_ms, 1)
    # Use real per-engine probe_ms from engine_metrics (not fabricated average)
    for ename, edata in data.get("engines", {}).items():
        edata["response_time_ms"] = edata.get("probe_ms", 0)
    with _cache_lock:
        _cache["engines"] = data
        _cache["engines_t"] = _time.time()
    return data

def _cached_windows():
    """Cached window scan (5s TTL)."""
    now = _time.time()
    with _cache_lock:
        if _cache["windows"] and (now - _cache["windows_t"]) < _WINDOWS_TTL:
            return _cache["windows"]
    from skynet_windows import scan_windows
    data = scan_windows()
    with _cache_lock:
        _cache["windows"] = data
        _cache["windows_t"] = _time.time()
    return data

def _build_dashboard_data():
    """Combined dashboard data: status + pulse + bus in one call."""
    now = _time.time()
    with _cache_lock:
        if _cache["dashboard_data"] and (now - _cache["dashboard_data_t"]) < _DASHBOARD_TTL:
            return _cache["dashboard_data"]
    # Fetch all three in parallel threads for speed
    results = {}
    errors = {}
    def _fetch(key, fn):
        try: results[key] = fn()
        except Exception as e: errors[key] = str(e)
    threads = [
        threading.Thread(target=_fetch, args=("status", _cached_backend_status)),
        threading.Thread(target=_fetch, args=("pulse", _cached_pulse)),
        threading.Thread(target=_fetch, args=("bus", lambda: _cached_bus(20))),
        threading.Thread(target=_fetch, args=("engines", _cached_engines)),
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=5)
    # Build engine summary from cached engine data
    eng_raw = results.get("engines", {})
    eng_map = eng_raw.get("engines", {})
    eng_online = sum(1 for e in eng_map.values() if e.get("status") == "online")
    eng_avail = sum(1 for e in eng_map.values() if e.get("status") == "available")
    eng_offline = sum(1 for e in eng_map.values() if e.get("status") == "offline")
    data = {
        "status": results.get("status", {}),
        "pulse": results.get("pulse", {}),
        "bus": results.get("bus", []),
        "consultants": results.get("status", {}).get("consultants", {}),
        "engines": {
            "online": eng_online,
            "available": eng_avail,
            "offline": eng_offline,
            "total": len(eng_map),
            "health_pct": round(eng_online / max(1, len(eng_map)) * 100),
        },
        "errors": errors if errors else None,
        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cached": True,
    }
    with _cache_lock:
        _cache["dashboard_data"] = data
        _cache["dashboard_data_t"] = _time.time()
    return data



# --------------- Background Precomputation ---------------
# Warms heavy caches (pulse, introspect, engines) so endpoints serve instantly.
_precompute_running = False

def _precompute_loop():
    """Background thread that precomputes expensive data every 30s."""
    global _precompute_running
    _precompute_running = True
    _time.sleep(2)  # let server start first
    while _precompute_running:
        try:
            _cached_pulse()
        except Exception as e:
            logger.debug("precompute: pulse failed: %s", e)
        try:
            _cached_engines()
        except Exception as e:
            logger.debug("precompute: engines failed: %s", e)
        # Precompute introspect (9s cold, cached 30s)
        reflection = None
        try:
            now = _time.time()
            with _cache_lock:
                stale = not _cache["introspect"] or (now - _cache["introspect_t"]) >= _INTROSPECT_TTL
            if stale:
                reflection = _get_skynet_self().introspection.reflect()
                with _cache_lock:
                    _cache["introspect"] = reflection
                    _cache["introspect_t"] = _time.time()
            else:
                with _cache_lock:
                    reflection = _cache["introspect"]
        except Exception as e:
            logger.debug("precompute: introspect failed: %s", e)
        # Precompute assess (uses introspect result, avoids double 9s call)
        try:
            now = _time.time()
            with _cache_lock:
                assess_stale = not _cache["assess"] or (now - _cache["assess_t"]) >= _INTROSPECT_TTL
            if assess_stale and reflection:
                sky = _get_skynet_self()
                text = sky._self_assessment(reflection)
                data = {"assessment": text, "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "version": "3.0", "level": 3}
                with _cache_lock:
                    _cache["assess"] = data
                    _cache["assess_t"] = _time.time()
        except Exception as e:
            logger.debug("precompute: assess failed: %s", e)
        _time.sleep(30)

def _start_precompute():
    t = threading.Thread(target=_precompute_loop, daemon=True, name="precompute")
    t.start()


# --------------- WebSocket Server (port 8423) ---------------
_ws_clients = set()
_ws_lock = threading.Lock()

def _ws_broadcast(data):
    """Broadcast data to all connected WebSocket clients."""
    msg = _dumps(data) if not isinstance(data, str) else data
    dead = set()
    with _ws_lock:
        for ws in _ws_clients.copy():
            try:
                import asyncio
                asyncio.run_coroutine_threadsafe(ws.send(msg), _ws_loop)
            except Exception:
                dead.add(ws)
        _ws_clients -= dead

_ws_loop = None

async def _ws_handler(websocket):
    with _ws_lock:
        _ws_clients.add(websocket)
    try:
        async for msg in websocket:
            # Client can send "ping" to get immediate data
            if msg.strip().lower() == "ping":
                try:
                    data = _build_dashboard_data()
                    await websocket.send(_dumps(data))
                except Exception as e:
                    await websocket.send(_dumps({"error": str(e)}))
    except Exception:
        pass
    finally:
        with _ws_lock:
            _ws_clients.discard(websocket)

async def _ws_push_loop():
    """Push updates to all WS clients every 3s."""
    while True:
        await asyncio.sleep(3)
        if not _ws_clients:
            continue
        try:
            data = _build_dashboard_data()
            msg = _dumps({"type": "update", "data": data})
            dead = set()
            with _ws_lock:
                for ws in _ws_clients.copy():
                    try:
                        await ws.send(msg)
                    except Exception:
                        dead.add(ws)
                _ws_clients -= dead
        except Exception:
            pass

def _start_ws_server():
    """Start WebSocket server on separate thread."""
    global _ws_loop
    import asyncio
    try:
        import websockets
    except ImportError:
        return  # No websockets library, skip
    async def _run():
        global _ws_loop
        _ws_loop = asyncio.get_event_loop()
        async with websockets.serve(_ws_handler, "127.0.0.1", WS_PORT):
            asyncio.ensure_future(_ws_push_loop())
            await asyncio.Future()  # run forever

    def _thread():
        asyncio.run(_run())
    t = threading.Thread(target=_thread, daemon=True)
    t.start()


# --------------- TODO Tracking System ---------------
_todos_lock = threading.Lock()

def _load_todos():
    """Load all worker TODOs from disk. Supports both list and dict formats."""
    if TODOS_FILE.exists():
        try:
            raw = json.loads(TODOS_FILE.read_text(encoding="utf-8"))
            # If it's the list format from Alpha ({"todos": [...], "version": N})
            if isinstance(raw, dict) and "todos" in raw and isinstance(raw["todos"], list):
                by_worker = {}
                for item in raw["todos"]:
                    w = item.get("worker", "unknown")
                    if w not in by_worker:
                        by_worker[w] = {"items": [], "updated": item.get("created_at", "")}
                    by_worker[w]["items"].append(item)
                    if item.get("created_at", "") > by_worker[w].get("updated", ""):
                        by_worker[w]["updated"] = item["created_at"]
                return {"by_worker": by_worker, "total": len(raw["todos"]), "raw": raw}
            return raw
        except (json.JSONDecodeError, OSError):
            pass
    return {"by_worker": {}, "total": 0}

def _save_todos(data):
    """Persist TODOs to disk atomically."""
    try:
        from tools.skynet_atomic import atomic_write_json
        atomic_write_json(TODOS_FILE, data)
    except (ModuleNotFoundError, ImportError):
        TODOS_FILE.parent.mkdir(parents=True, exist_ok=True)
        TODOS_FILE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

def _update_worker_todos(sender, items):
    """Update a single worker's TODO list, merging with existing data."""
    import hashlib
    with _todos_lock:
        if TODOS_FILE.exists():
            try:
                raw = json.loads(TODOS_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {"todos": [], "version": 1}
        else:
            raw = {"todos": [], "version": 1}
        # Ensure list format
        if "todos" not in raw or not isinstance(raw.get("todos"), list):
            raw = {"todos": [], "version": 1}
        # Remove old items for this sender
        raw["todos"] = [t for t in raw["todos"] if t.get("worker") != sender]
        # Add new items
        ts = _time.strftime("%Y-%m-%dT%H:%M:%SZ")
        for item in items:
            if isinstance(item, str):
                entry = {
                    "id": hashlib.md5(f"{sender}{item}".encode()).hexdigest()[:8],
                    "worker": sender,
                    "task": item,
                    "status": "pending",
                    "priority": "normal",
                    "created_at": ts,
                    "completed_at": None,
                }
            elif isinstance(item, dict):
                entry = item
                entry.setdefault("worker", sender)
                entry.setdefault("id", hashlib.md5(f"{sender}{item.get('task','')}".encode()).hexdigest()[:8])
            else:
                continue
            raw["todos"].append(entry)
        raw["version"] = raw.get("version", 0) + 1
        _save_todos(raw)
    return raw

def _start_bus_todo_listener():
    """Background thread that captures TODO updates from bus messages."""
    def _poll():
        last_seen_id = ""
        while True:
            try:
                msgs = _fetch_backend("http://localhost:8420/bus/messages?limit=50", timeout=2)
                if isinstance(msgs, list):
                    for m in msgs:
                        mid = m.get("id", "")
                        if mid <= last_seen_id:
                            continue
                        if m.get("topic") == "todos" and m.get("type") == "update":
                            sender = m.get("sender", "unknown")
                            try:
                                content = m.get("content", "{}")
                                if isinstance(content, str):
                                    content = json.loads(content)
                                items = content.get("items", []) if isinstance(content, dict) else []
                            except (json.JSONDecodeError, AttributeError):
                                items = [str(content)]
                            _update_worker_todos(sender, items)
                        last_seen_id = max(last_seen_id, mid)
            except Exception:
                pass
            _time.sleep(5)
    t = threading.Thread(target=_poll, daemon=True)
    t.start()


def _load_learning_episodes(base):
    """Load learning episodes; return (last_500, total_count)."""
    ep_file = base / "data" / "learning_episodes.json"
    if not ep_file.exists():
        return [], 0
    try:
        raw = json.loads(ep_file.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return raw[-500:], len(raw)
    except Exception:
        pass
    return [], 0


def _count_episode_outcomes(episodes):
    outcomes = {"success": 0, "failure": 0, "unknown": 0}
    for ep in episodes:
        o = ep.get("outcome", "unknown")
        if o in outcomes:
            outcomes[o] += 1
        else:
            outcomes["unknown"] += 1
    return outcomes


def _add_learning_store_stats(metrics):
    try:
        from core.learning_store import LearningStore
        store = LearningStore()
        store_stats = store.stats()
        metrics["total_facts"] = store_stats.get("total_facts", 0)
        metrics["avg_confidence"] = round(store_stats.get("average_confidence", 0.0), 3)
        metrics["by_category"] = store_stats.get("by_category", {})
    except Exception:
        metrics["total_facts"] = 0
        metrics["avg_confidence"] = 0.0
        metrics["by_category"] = {}


def _check_learner_daemon(base):
    pid_file = base / "data" / "learner.pid"
    try:
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            return "running" if _pid_alive(pid) else "stopped"
    except (OSError, ValueError):
        pass
    return "stopped"


def _build_episode_sparkline(episodes, now):
    """Build hourly episode sparkline for the last 24 hours."""
    import datetime
    buckets = [0] * 24
    for ep in episodes:
        ts = ep.get("timestamp_iso") or ep.get("timestamp", "")
        if not ts:
            continue
        try:
            dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            age_h = (now - dt.timestamp()) / 3600
            if 0 <= age_h < 24:
                buckets[int(age_h)] += 1
        except Exception:
            pass
    return list(reversed(buckets))


class ConsoleHandler(SimpleHTTPRequestHandler):
    """Serves the GOD Console HTML dashboard."""

    def setup(self):
        """Disable Nagle algorithm for low-latency responses."""
        super().setup()
        import socket
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def address_string(self):
        """Override to skip reverse DNS lookup (saves ~2s per request)."""
        return self.client_address[0]

    def do_GET(self):
        t0 = _time.time()
        status_code = 200
        try:
            self._route(t0)
        except Exception as e:
            status_code = 500
            logger.error("GET %s failed: %s\n%s", self.path, e, traceback.format_exc())
            self._json_response({"error": str(e), "endpoint": self.path}, status=500)
        elapsed_ms = (_time.time() - t0) * 1000
        self._log_access(self.path, status_code, elapsed_ms)

    def _route(self, t0):
        if self.path in ("/", "/index.html", "/god", "/god_console.html"):
            self.send_response(302)
            self.send_header("Location", "/dashboard")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
        elif self.path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
        elif self.path == "/version":
            self._json_response({"version": "3.0", "level": 3, "codename": "Level 3", "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ")})  # signed: delta
        elif self.path == "/health":
            endpoints = ["/", "/dashboard", "/engines", "/version", "/health",
                         "/skynet/self/pulse", "/skynet/self/status",
                         "/skynet/self/introspect", "/skynet/self/goals",
                         "/skynet/self/assess", "/skynet/status",
                         "/status", "/god_state", "/bus", "/bus/tasks", "/bus/convene", "/bus/stats",
                         "/windows", "/workers/health", "/dashboard/data", "/ws/info", "/todos", "/consultants",
                         "/processes", "/overseer", "/stream/dashboard",
                         "/kill/pending", "/kill/log", "/learner/health",
                         "/missions/active", "/system/health", "/metrics/throughput",
                         "/leadership"]  # signed: beta
            self._json_response({
                "status": "ok",
                "uptime_s": round(_time.time() - _SERVER_START, 1),
                "endpoints_active": len(endpoints),
                "pid": os.getpid(),
                "ws_port": WS_PORT,
                "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ"),  # signed: delta
            })
        elif self.path == "/bus" or self.path.startswith("/bus?") or self.path.startswith("/bus/messages"):
            limit = 20
            if "?" in self.path:
                import urllib.parse
                qs = urllib.parse.parse_qs(self.path.split("?", 1)[1])
                try:
                    limit = max(1, min(1000, int(qs.get("limit", [20])[0])))
                except (ValueError, TypeError):
                    limit = 20
            data = _cached_bus(limit)
            self._json_response(data)
        elif self.path == "/bus/tasks":
            data = _fetch_backend("http://localhost:8420/bus/tasks")
            self._json_response(data)
        elif self.path == "/bus/convene":
            data = _fetch_backend("http://localhost:8420/bus/convene")
            self._json_response(data)
        elif self.path == "/bus/stats":
            backend_metrics = _fetch_backend("http://localhost:8420/metrics", timeout=2)
            stats = {
                "bus_depth": backend_metrics.get("bus_depth", 0) if isinstance(backend_metrics, dict) else 0,
                "bus_messages_total": backend_metrics.get("bus_messages_total", 0) if isinstance(backend_metrics, dict) else 0,
                "bus_dropped": backend_metrics.get("bus_dropped", 0) if isinstance(backend_metrics, dict) else 0,
                "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            self._json_response(stats)
        elif self.path == "/dashboard":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.read_bytes())
        elif self.path == "/dashboard/data":
            data = _build_dashboard_data()
            self._json_response(data)
        elif self.path == "/engines":
            data = _cached_engines()
            self._json_response(data)
        elif self.path == "/skynet/self/pulse":
            try:
                data = _cached_pulse()
            except Exception as e:
                data = {"error": str(e)}
            self._json_response(data)
        elif self.path == "/skynet/self/status":
            try:
                data = _cached_status()
            except Exception as e:
                try:
                    data = _cached_pulse()
                    data["partial"] = True
                except Exception:
                    data = {"error": str(e)}
            self._json_response(data)
        elif self.path == "/skynet/self/introspect":
            now = _time.time()
            with _cache_lock:
                cached = _cache["introspect"] if _cache["introspect"] and (now - _cache["introspect_t"]) < _INTROSPECT_TTL else None
            if cached:
                data = cached
            else:
                try:
                    data = _get_skynet_self().introspection.reflect()
                    with _cache_lock:
                        _cache["introspect"] = data
                        _cache["introspect_t"] = _time.time()
                except Exception as e:
                    data = _cache["introspect"] or {"error": str(e)}
            self._json_response(data)
        elif self.path == "/skynet/self/goals":
            try:
                data = _get_skynet_self().goals.suggest()
            except Exception as e:
                data = {"error": str(e)}
            self._json_response(data)
        elif self.path == "/skynet/self/assess":
            now = _time.time()
            with _cache_lock:
                cached = _cache["assess"] if _cache["assess"] and (now - _cache["assess_t"]) < _INTROSPECT_TTL else None
            if cached:
                data = cached
            else:
                try:
                    sky = _get_skynet_self()
                    # Reuse cached introspect if available
                    with _cache_lock:
                        reflection = _cache["introspect"] if _cache["introspect"] else None
                    if not reflection:
                        reflection = sky.introspection.reflect()
                    text = sky._self_assessment(reflection)
                    data = {"assessment": text, "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "version": "3.0", "level": 3}
                    with _cache_lock:
                        _cache["assess"] = data
                        _cache["assess_t"] = _time.time()
                except Exception as e:
                    data = {"error": str(e), "assessment": None}
            self._json_response(data)
        elif self.path == "/skynet/status":
            try:
                pulse = _cached_pulse()
                health = pulse.get("health", "UNKNOWN")
                iq = pulse.get("intelligence_score", 0)
                alive = pulse.get("alive", 0)
                total = pulse.get("total", 5)
                eng_on = pulse.get("engines_online", 0)
                eng_tot = pulse.get("engines_total", 0)
                line = f"SKYNET v3.0 Level 3 | {health} | IQ {iq} | {alive}/{total} workers | {eng_on}/{eng_tot} engines"
                data = {"status_line": line, "health": health, "iq": iq, "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ")}  # signed: delta
            except Exception as e:
                data = {"status_line": f"SKYNET v3.0 Level 3 | ERROR: {e}", "health": "ERROR", "iq": 0, "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ")}
            self._json_response(data)
        elif self.path == "/status":
            try:
                backend = _cached_backend_status()
            except Exception:
                backend = {"agents": {}}
            try:
                pulse = _cached_pulse()
                backend["self_aware"] = True
                backend["pulse"] = pulse
                backend["collective_iq"] = pulse.get("intelligence_score", 0)
            except Exception as e:
                backend["self_aware"] = False
                backend["error"] = str(e)
            backend["timestamp"] = _time.strftime("%Y-%m-%dT%H:%M:%SZ")  # signed: delta
            self._json_response(backend)
        elif self.path == "/god_state":
            try:
                pulse = _cached_pulse()
                from skynet_collective import intelligence_score
                iq = intelligence_score()
                health = pulse.get("health", "UNKNOWN")
                iq_score = iq.get("intelligence_score", 0)
                briefing = f"System Health: {health} | Collective IQ: {iq_score:.3f}"
                data = {"briefing": briefing, "health": health, "collective_iq": iq_score, "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ")}  # signed: delta
            except Exception as e:
                data = {"briefing": f"Self-awareness unavailable: {e}", "health": "UNKNOWN", "collective_iq": 0, "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ")}
            self._json_response(data)
        elif self.path == "/windows":
            try:
                data = _cached_windows()
            except Exception as e:
                data = {"error": str(e)}
            if isinstance(data, dict):
                data.setdefault("timestamp", _time.strftime("%Y-%m-%dT%H:%M:%SZ"))  # signed: delta
            self._json_response(data)
        elif self.path == "/workers/health":
            try:
                from skynet_stuck_detector import get_worker_health_json
                data = get_worker_health_json()
            except Exception as e:
                data = {"error": str(e), "workers": {}}
            if isinstance(data, dict):
                data.setdefault("timestamp", _time.strftime("%Y-%m-%dT%H:%M:%SZ"))  # signed: delta
            self._json_response(data)
        elif self.path == "/learner/health":
            self._handle_learner_health()
        elif self.path == "/learner/metrics":
            self._handle_learner_metrics()
        elif self.path == "/ws/info":
            self._json_response({"ws_url": f"ws://localhost:{WS_PORT}", "protocol": "websocket", "fallback": "polling", "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ")})  # signed: delta
        elif self.path == "/missions/active":
            try:
                sys.path.insert(0, str(Path(__file__).parent / "tools"))
                from skynet_missions import MissionControl
                mc = MissionControl()
                active = mc.active_missions()
                missions_list = mc.to_dict_list(active)
                # Also proxy Go backend /tasks for real dispatch-level tasks  # signed: beta
                backend_tasks = []
                try:
                    raw = _fetch_backend("http://localhost:8420/tasks", timeout=2, retries=0)
                    if isinstance(raw, list):
                        backend_tasks = raw
                    elif isinstance(raw, dict) and "tasks" in raw:
                        backend_tasks = raw["tasks"]
                except Exception:
                    pass
                self._json_response({
                    "missions": missions_list,
                    "count": len(missions_list),
                    "backend_tasks": backend_tasks,
                    "stats": mc.stats(),
                    "timeline": mc.get_mission_timeline(),
                    "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ"),  # signed: beta
                })
            except Exception as e:
                # Fallback: try Go backend /tasks directly  # signed: beta
                backend_tasks = []
                try:
                    raw = _fetch_backend("http://localhost:8420/tasks", timeout=2, retries=0)
                    if isinstance(raw, list):
                        backend_tasks = raw
                    elif isinstance(raw, dict) and "tasks" in raw:
                        backend_tasks = raw["tasks"]
                except Exception:
                    pass
                self._json_response({"error": str(e), "missions": [], "backend_tasks": backend_tasks, "count": 0, "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ")})
        elif self.path == "/system/health":
            try:
                sys.path.insert(0, str(Path(__file__).parent / "tools"))
                from skynet_observability import system_health
                self._json_response(system_health())
            except Exception as e:
                self._json_response({"status": "error", "error": str(e), "issues": [str(e)], "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ")})
        elif self.path == "/metrics/throughput":
            try:
                sys.path.insert(0, str(Path(__file__).parent / "tools"))
                from skynet_observability import throughput_metrics
                self._json_response(throughput_metrics())
            except Exception as e:
                self._json_response({"error": str(e), "total_dispatches": 0})
        elif self.path == "/todos":
            data = _load_todos()
            if isinstance(data, dict):
                data.setdefault("timestamp", _time.strftime("%Y-%m-%dT%H:%M:%SZ"))  # signed: delta
            self._json_response(data)
        elif self.path == "/consultants":
            data = _cached_consultants()
            if isinstance(data, dict):
                data.setdefault("timestamp", _time.strftime("%Y-%m-%dT%H:%M:%SZ"))  # signed: delta
            self._json_response(data)
        elif self.path == "/processes":
            try:
                from skynet_process_guard import _load_registry
                data = _load_registry()
            except Exception as e:
                data = {"error": str(e), "processes": []}
            if isinstance(data, dict):
                data.setdefault("timestamp", _time.strftime("%Y-%m-%dT%H:%M:%SZ"))  # signed: delta
            self._json_response(data)
        elif self.path == "/overseer":
            status_file = os.path.join(os.path.dirname(__file__), "data", "overseer_status.json")
            pid_file = os.path.join(os.path.dirname(__file__), "data", "overseer.pid")
            result = {"running": False, "pid": None, "status": None}
            if os.path.exists(pid_file):
                try:
                    pid = int(open(pid_file).read().strip())
                    result["pid"] = pid
                    result["running"] = _pid_alive(pid)
                except Exception:
                    pass
            if os.path.exists(status_file):
                try:
                    result["status"] = json.loads(open(status_file, encoding="utf-8").read())
                except Exception:
                    pass
            result["timestamp"] = _time.strftime("%Y-%m-%dT%H:%M:%SZ")  # signed: delta
            self._json_response(result)
        elif self.path == "/incidents":
            inc_file = os.path.join(os.path.dirname(__file__), "data", "incidents.json")
            if os.path.exists(inc_file):
                try:
                    data = json.loads(open(inc_file, encoding="utf-8").read())
                    self._json_response(data)
                except Exception:
                    self._json_response([])
            else:
                self._json_response([])
        elif self.path == "/task/pending" or self.path.startswith("/task/pending?"):
            try:
                from skynet_task_tracker import get_pending
                worker = None
                if "?" in self.path:
                    qs = self.path.split("?", 1)[1]
                    for part in qs.split("&"):
                        if part.startswith("worker="):
                            worker = part.split("=", 1)[1]
                # Validate worker name to prevent path traversal  # signed: beta
                if worker is not None and not worker.isalnum():
                    self._json_response({"error": "invalid worker name"}, status=400)
                    return
                self._json_response(get_pending(worker))
            except Exception as e:
                self._json_response({"error": str(e)}, status=500)
        elif self.path == "/task/summary":
            try:
                from skynet_task_tracker import get_summary
                self._json_response(get_summary())
            except Exception as e:
                self._json_response({"error": str(e)}, status=500)
        elif self.path.startswith("/task/get/"):
            try:
                from skynet_task_tracker import get_task
                tid = self.path.split("/task/get/", 1)[1]
                # Validate task ID: alphanumeric, hyphens, underscores only  # signed: beta
                import re as _re_tid
                if not _re_tid.match(r'^[a-zA-Z0-9_-]+$', tid):
                    self._json_response({"error": "invalid task id"}, status=400)
                    return
                rec = get_task(tid)
                if rec:
                    self._json_response(rec)
                else:
                    self._json_response({"error": "not found"}, status=404)
            except Exception as e:
                self._json_response({"error": str(e)}, status=500)
        elif self.path.startswith("/task/can-stop/"):
            try:
                from skynet_task_tracker import can_stop
                worker = self.path.split("/task/can-stop/", 1)[1]
                # Validate worker name: alphanumeric only  # signed: beta
                if not worker.isalnum():
                    self._json_response({"error": "invalid worker name"}, status=400)
                    return
                ok, count, tasks = can_stop(worker)
                self._json_response({"can_stop": ok, "pending": count, "tasks": tasks})
            except Exception as e:
                self._json_response({"error": str(e)}, status=500)
        elif self.path == "/kill/pending":
            self._handle_kill_pending()
        elif self.path == "/kill/log" or self.path.startswith("/kill/log?"):
            try:
                sys.path.insert(0, str(Path(__file__).parent / "tools"))
                from skynet_kill_auth import get_kill_log
                limit = 20
                if "?" in self.path:
                    import urllib.parse
                    qs = urllib.parse.parse_qs(self.path.split("?", 1)[1])
                    try:
                        limit = max(1, min(1000, int(qs.get("limit", [20])[0])))  # Cap at 1000  # signed: beta
                    except (ValueError, TypeError):
                        limit = 20
                self._json_response(get_kill_log(limit))
            except Exception as e:
                self._json_response({"error": str(e)}, status=500)
        elif self.path == "/stream/dashboard":
            self._handle_stream_dashboard()
            return
        elif self.path.startswith("/worker/") and self.path.endswith("/performance"):
            # GET /worker/{name}/performance
            parts = self.path.split("/")
            worker_name = parts[2] if len(parts) >= 4 else ""
            try:
                sys.path.insert(0, str(Path(__file__).parent / "tools"))
                from skynet_smart_router import get_worker_performance
                data = get_worker_performance(worker_name)
                if data:
                    self._json_response(data)
                else:
                    self._json_response({"error": f"no data for worker {worker_name}"}, status=404)
            except Exception as e:
                self._json_response({"error": str(e)}, status=500)
        elif self.path == "/performance/leaderboard":
            try:
                sys.path.insert(0, str(Path(__file__).parent / "tools"))
                from skynet_smart_router import get_leaderboard
                board = get_leaderboard()
                self._json_response({"leaderboard": board, "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ")})
            except Exception as e:
                self._json_response({"error": str(e)}, status=500)
        elif self.path == "/api/workers/performance":
            try:
                sys.path.insert(0, str(Path(__file__).parent / "tools"))
                from skynet_smart_router import get_leaderboard, get_worker_performance
                board = get_leaderboard()
                detailed = {}
                for entry in board:
                    wp = get_worker_performance(entry["worker"])
                    if wp:
                        detailed[entry["worker"]] = wp
                self._json_response({
                    "leaderboard": board,
                    "workers": detailed,
                    "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
            except Exception as e:
                self._json_response({"error": str(e)}, status=500)
        elif self.path == "/api/ci/latest":
            try:
                ci_path = Path(__file__).parent / "data" / "ci_report.json"
                if ci_path.exists():
                    ci_data = json.loads(ci_path.read_text(encoding="utf-8"))
                    self._json_response(ci_data)
                else:
                    self._json_response({
                        "status": "no_data",
                        "message": "No CI report found at data/ci_report.json",
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    })
            except Exception as e:
                self._json_response({"error": str(e)}, status=500)
        elif self.path == "/api/worker/activity":
            self._handle_worker_activity_all()
        elif self.path.startswith("/api/worker/") and self.path.endswith("/thinking"):
            parts = self.path.split("/")
            worker_name = parts[3] if len(parts) >= 5 else ""
            self._handle_worker_thinking(worker_name)
        elif self.path == "/leadership":
            self._handle_leadership()
        else:
            self.send_error(404)

    def _handle_learner_health(self):
        """GET /learner/health — cached learner daemon status."""
        now = _time.time()
        with _cache_lock:
            if _learner_cache["health"] and (now - _learner_cache["health_t"]) < _LEARNER_HEALTH_TTL:
                _learner_cache["health_hits"] += 1
                data = dict(_learner_cache["health"])
                data["cache_age_ms"] = int((now - _learner_cache["health_t"]) * 1000)
                data["cache_hit"] = True
                data["cache_hits"] = _learner_cache["health_hits"]
                data["cache_misses"] = _learner_cache["health_misses"]
                self._json_response(data)
                return
            _learner_cache["health_misses"] += 1
            cache_hits = _learner_cache["health_hits"]
            cache_misses = _learner_cache["health_misses"]
        data = self._probe_learner_daemon()
        with _cache_lock:
            _learner_cache["health"] = data
            _learner_cache["health_t"] = now
        data["cache_age_ms"] = 0
        data["cache_hit"] = False
        data["cache_hits"] = cache_hits
        data["cache_misses"] = cache_misses
        self._json_response(data)

    @staticmethod
    def _probe_learner_daemon():
        """Probe the learner daemon status from PID file and state file."""
        now = _time.time()
        try:
            pid_file = Path(__file__).resolve().parent / "data" / "learner.pid"
            if pid_file.exists():
                pid = int(pid_file.read_text().strip())
                if not _pid_alive(pid):
                    raise OSError(f"learner pid {pid} is not alive")
                state_file = Path(__file__).resolve().parent / "data" / "learner_state.json"
                state = json.loads(state_file.read_text()) if state_file.exists() else {}
                last_run = state.get("last_run")
                stale_seconds = -1
                if last_run:
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        lr_ts = _dt.fromisoformat(last_run).replace(tzinfo=_tz.utc).timestamp()
                        stale_seconds = int(now - lr_ts)
                    except (ValueError, TypeError):
                        stale_seconds = -1
                stale = stale_seconds > 300 if stale_seconds >= 0 else True
                return {
                    "status": "running", "pid": pid,
                    "episodes_processed": state.get("total_processed", 0),
                    "total_learnings": state.get("total_learnings", 0),
                    "last_run": last_run, "started_at": state.get("started_at"),
                    "stale": stale, "stale_seconds": stale_seconds,
                }
            else:
                return {"status": "stopped", "pid": None, "stale": True, "stale_seconds": -1}
        except (OSError, ValueError):
            return {"status": "stopped", "pid": None, "stale": True, "stale_seconds": -1}
        except Exception as e:
            return {"status": "error", "error": str(e), "stale": True, "stale_seconds": -1}

    def _handle_learner_metrics(self):
        """GET /learner/metrics — learning telemetry (cached 5s)."""
        now = _time.time()
        with _cache_lock:
            if _learner_cache["metrics"] and (now - _learner_cache["metrics_t"]) < _LEARNER_METRICS_TTL:
                _learner_cache["metrics_hits"] += 1
                data = dict(_learner_cache["metrics"])
                data["cache_age_ms"] = int((now - _learner_cache["metrics_t"]) * 1000)
                data["cache_hit"] = True
                data["cache_hits"] = _learner_cache["metrics_hits"]
                data["cache_misses"] = _learner_cache["metrics_misses"]
                self._json_response(data)
                return
            _learner_cache["metrics_misses"] += 1
            cache_hits = _learner_cache["metrics_hits"]
            cache_misses = _learner_cache["metrics_misses"]
        try:
            metrics = self._collect_learner_metrics(now)
            with _cache_lock:
                _learner_cache["metrics"] = metrics
                _learner_cache["metrics_t"] = now
            metrics["cache_age_ms"] = 0
            metrics["cache_hit"] = False
            metrics["cache_hits"] = cache_hits
            metrics["cache_misses"] = cache_misses
            self._json_response(metrics)
        except Exception as e:
            self._json_response({"error": str(e), "total_episodes": 0, "by_outcome": {"success": 0, "failure": 0, "unknown": 0}, "sparkline_hourly": [], "total_facts": 0, "daemon_status": "error", "timestamp": now, "cache_age_ms": 0, "cache_hit": False}, status=200)

    @staticmethod
    def _collect_learner_metrics(now):
        """Build learner telemetry metrics from episode file and LearningStore."""
        base = Path(__file__).resolve().parent
        metrics = {"timestamp": now}

        episodes, total_ep_count = _load_learning_episodes(base)
        metrics["total_episodes"] = total_ep_count
        metrics["by_outcome"] = _count_episode_outcomes(episodes)

        if episodes:
            last = episodes[-1]
            metrics["last_episode_ts"] = last.get("timestamp_iso") or last.get("timestamp")
            metrics["last_episode_worker"] = last.get("worker")
        else:
            metrics["last_episode_ts"] = None
            metrics["last_episode_worker"] = None

        metrics["sparkline_hourly"] = _build_episode_sparkline(episodes, now)
        _add_learning_store_stats(metrics)
        metrics["daemon_status"] = _check_learner_daemon(base)
        return metrics

    def _handle_kill_pending(self):
        """GET /kill/pending — pending kill requests with vote enrichment."""
        try:
            sys.path.insert(0, str(Path(__file__).parent / "tools"))
            from skynet_kill_auth import get_pending_requests
            raw = get_pending_requests()
            votes_by_req = self._collect_kill_votes()
            requests_list = []
            for p in raw:
                rid = p.get("request_id", "")
                ts = p.get("timestamp", "")
                epoch = 0
                try:
                    from datetime import datetime as _dt
                    epoch = _dt.fromisoformat(ts).timestamp()
                except Exception:
                    epoch = _time.time()
                requests_list.append({
                    "id": rid, "pid": p.get("pid"),
                    "name": p.get("name", "unknown"),
                    "reason": p.get("reason", ""),
                    "requester": p.get("requester", "?"),
                    "timestamp": epoch,
                    "status": p.get("status", "voting"),
                    "votes": votes_by_req.get(rid, {}),
                })
            self._json_response({"requests": requests_list})
        except Exception as e:
            self._json_response({"requests": [], "error": str(e)})

    def _collect_kill_votes(self):
        """Collect kill consensus votes from cached bus messages."""
        votes_by_req = {}
        try:
            msgs = _cached_bus(100)
            if isinstance(msgs, list):
                for m in msgs:
                    if m.get("type") == "kill_consensus_vote":
                        try:
                            c = m.get("content", "")
                            if isinstance(c, str):
                                c = json.loads(c)
                            rid = c.get("request_id", "")
                            worker = c.get("worker", "")
                            if rid and worker:
                                votes_by_req.setdefault(rid, {})[worker] = {
                                    "safe": c.get("safe", False),
                                    "reason": c.get("reason", ""),
                                }
                        except Exception:
                            pass
        except Exception:
            pass
        return votes_by_req

    def _handle_stream_dashboard(self):
        """SSE /stream/dashboard — push aggregated dashboard data every 2s."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            while True:
                payload = self._build_sse_payload()
                line = f"data: {json.dumps(payload, default=str)}\n\n"
                self.wfile.write(line.encode())
                self.wfile.flush()
                _time.sleep(2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    @staticmethod
    def _build_sse_payload():
        """Build the SSE dashboard payload from cached sources."""
        payload = {}
        for key, fetcher in [("status", _cached_backend_status),
                              ("consultants", _cached_consultants),
                              ("bus", lambda: _cached_bus(20)),
                              ("todos", _load_todos)]:
            try:
                payload[key] = fetcher()
            except Exception:
                payload[key] = None
        # Include engine summary counts for real-time dashboard  # signed: beta
        try:
            eng = _cached_engines()
            summary = eng.get("summary", {})
            payload["engines"] = {
                "online": summary.get("online", 0),
                "available": summary.get("available", 0),
                "offline": summary.get("offline", summary.get("total", 0) - summary.get("online", 0) - summary.get("available", 0)),
                "total": summary.get("total", 0),
                "health_pct": summary.get("health_pct", 0),
            }
        except Exception:
            payload["engines"] = None
        try:
            sf = os.path.join(os.path.dirname(__file__), "data", "overseer_status.json")
            if os.path.exists(sf):
                with open(sf, encoding="utf-8") as _f:
                    payload["overseer"] = json.loads(_f.read())
        except Exception:
            pass
        # Cache-age disclosure per Truth Standards  # signed: gamma
        now = _time.time()
        cache_ages = {}
        with _cache_lock:
            for key, tkey in [("status", "backend_status_t"),
                               ("consultants", "consultants_t"),
                               ("bus", "bus_t"),
                               ("engines", "engines_t")]:
                ct = _cache.get(tkey, 0)
                cache_ages[key] = round(now - ct, 1) if ct else None
        payload["cache_ages"] = cache_ages
        payload["server_ts"] = now
        return payload

    def _handle_worker_activity_all(self):
        """GET /api/worker/activity — all worker activity (cached 2s)."""
        global _activity_file_cache, _activity_file_cache_t
        now = _time.time()
        with _cache_lock:
            if _activity_file_cache and (now - _activity_file_cache_t) < _ACTIVITY_TTL:
                self._json_response(_activity_file_cache)
                return
        activity_file = Path(__file__).parent / "data" / "worker_activity.json"
        workers = ("alpha", "beta", "gamma", "delta")
        unknown = {"state": "unknown", "current_activity": None, "last_tool": None,
                    "last_file": None, "timestamp": None, "recent_activities": []}
        try:
            data = json.loads(activity_file.read_text(encoding="utf-8")) if activity_file.exists() else {}
            for wn, wdata in _activity_cache.items():
                if wn not in data or (wdata.get("timestamp", "") > (data.get(wn, {}).get("timestamp", "") or "")):
                    data[wn] = wdata
            for wn in workers:
                if wn not in data:
                    data[wn] = dict(unknown)
            with _cache_lock:
                _activity_file_cache = data
                _activity_file_cache_t = _time.time()
            self._json_response(data)
        except Exception:
            self._json_response({wn: dict(unknown) for wn in workers})

    def _handle_worker_thinking(self, worker_name):
        """GET /api/worker/{name}/thinking — worker thinking state."""
        if worker_name not in ("alpha", "beta", "gamma", "delta"):
            self._json_response({"error": f"unknown worker: {worker_name}"}, status=400)
            return
        activity_file = Path(__file__).parent / "data" / "worker_activity.json"
        try:
            wdata = {}
            if activity_file.exists():
                all_data = json.loads(activity_file.read_text(encoding="utf-8"))
                wdata = all_data.get(worker_name, {})
            mem = _activity_cache.get(worker_name, {})
            if mem.get("timestamp", "") > (wdata.get("timestamp", "") or ""):
                wdata = mem
            now_ts = _time.time()
            last_ts = wdata.get("epoch", 0)
            self._json_response({
                "worker": worker_name,
                "state": wdata.get("state", "unknown"),
                "thinking": wdata.get("thinking", wdata.get("thinking_summary", None)),
                "current_tool": wdata.get("last_tool", wdata.get("current_tool", None)),
                "last_activity_ago_s": round(now_ts - last_ts, 1) if last_ts else None,
                "recent": (wdata.get("recent_activities") or [])[-10:],
            })
        except Exception as e:
            self._json_response({"error": str(e)}, status=500)

    def _handle_leadership(self):
        """GET /leadership — consolidated status for orchestrator + consultants.
        Real liveness probes against orchestrator (8420), Codex (8422), Gemini (8425).
        Returns truthful status — only 'live' if probe succeeds.
        """  # signed: beta
        from urllib.request import urlopen
        from urllib.error import URLError

        def _probe_port(port, label, timeout=1.0):
            """Probe a service port and return truthful status."""
            result = {"label": label, "port": port, "live": False, "status": "offline"}
            try:
                raw = urlopen(f"http://localhost:{port}/health", timeout=timeout).read()
                health = json.loads(raw)
                result["live"] = True
                result["status"] = "live"
                result["health"] = health
            except (URLError, OSError, json.JSONDecodeError):
                # Port not responding or health endpoint missing
                try:
                    # Fallback: try /status for the orchestrator backend
                    raw = urlopen(f"http://localhost:{port}/status", timeout=timeout).read()
                    result["live"] = True
                    result["status"] = "live"
                except (URLError, OSError):
                    result["live"] = False
                    result["status"] = "offline"
            return result

        # Probe orchestrator backend (Go backend on 8420)
        orch = _probe_port(8420, "orchestrator_backend")
        # Read orchestrator HWND/identity from data file
        orch_json = Path(__file__).parent / "data" / "orchestrator.json"
        if orch_json.exists():
            try:
                orch["identity"] = json.loads(orch_json.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Probe Codex consultant bridge (8422)
        codex = _probe_port(8422, "codex_consultant")
        codex_state = Path(__file__).parent / "data" / "consultant_state.json"
        if codex_state.exists():
            try:
                cs = json.loads(codex_state.read_text(encoding="utf-8"))
                codex["state_file"] = {
                    "sender": cs.get("sender", "consultant"),
                    "last_heartbeat": cs.get("last_heartbeat"),
                    "model": cs.get("model"),
                }
            except Exception:
                pass

        # Probe Gemini consultant bridge (8425)
        gemini = _probe_port(8425, "gemini_consultant")
        gemini_state = Path(__file__).parent / "data" / "gemini_consultant_state.json"
        if gemini_state.exists():
            try:
                gs = json.loads(gemini_state.read_text(encoding="utf-8"))
                gemini["state_file"] = {
                    "sender": gs.get("sender", "gemini_consultant"),
                    "last_heartbeat": gs.get("last_heartbeat"),
                    "model": gs.get("model"),
                }
            except Exception:
                pass

        # Worker count from backend
        worker_count = 0
        try:
            backend = _cached_backend_status()
            agents = backend.get("agents", {})
            worker_count = len(agents)
        except Exception:
            pass

        live_count = sum(1 for s in [orch, codex, gemini] if s["live"])
        self._json_response({
            "orchestrator": orch,
            "codex_consultant": codex,
            "gemini_consultant": gemini,
            "worker_count": worker_count,
            "leadership_live": live_count,
            "leadership_total": 3,
            "overall_status": "healthy" if live_count >= 2 else ("degraded" if live_count >= 1 else "offline"),
            "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })  # signed: beta

    MAX_POST_SIZE = 10 * 1024 * 1024  # 10 MB  # signed: beta

    def do_POST(self):
        t0 = _time.time()
        status_code = 200
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > self.MAX_POST_SIZE:
                self._json_response({"error": "payload too large"}, status=413)
                self._log_access(f"POST {self.path}", 413, (_time.time() - t0) * 1000)
                return
            body = self.rfile.read(length) if length else b"{}"
            data = json.loads(body)
            status_code = self._route_post(data, body)
        except Exception as e:
            status_code = 500
            logger.error("POST %s failed: %s\n%s", self.path, e, traceback.format_exc())
            self._json_response({"error": str(e)}, status=500)
        self._log_access(f"POST {self.path}", status_code, (_time.time() - t0) * 1000)

    def _route_post(self, data, body):
        """Dispatch POST requests to handlers. Returns HTTP status code."""
        if self.path == "/todos":
            sender = data.get("sender", "unknown")
            items = data.get("items", [])
            result = _update_worker_todos(sender, items)
            self._json_response({"ok": True, "sender": sender, "total_workers": len(result)})
            return 200
        elif self.path == "/bus/publish":
            return self._post_proxy_bus_publish(body)
        elif self.path == "/bus/task":
            return self._post_bus_task(data)
        elif self.path == "/dispatch":
            return self._post_dispatch(data)
        elif self.path == "/task/create":
            return self._post_task_create(data)
        elif self.path == "/task/update":
            return self._post_task_update(data)
        elif self.path == "/kill/authorize":
            return self._post_kill_authorize(data)
        elif self.path == "/kill/deny":
            return self._post_kill_deny(data)
        elif self.path.startswith("/worker/") and self.path.endswith("/metrics"):
            return self._post_worker_metrics(data)
        elif self.path.startswith("/api/worker/") and self.path.endswith("/activity"):
            return self._post_worker_activity(data)
        else:
            self._json_response({"error": "not found"}, status=404)
            return 404

    def _post_proxy_bus_publish(self, body):
        import urllib.request
        req = urllib.request.Request(
            "http://localhost:8420/bus/publish", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                self._json_response(json.loads(resp.read()))
            return 200
        except Exception as proxy_err:
            self._json_response({"error": f"proxy failed: {proxy_err}"}, status=502)
            return 502

    @staticmethod
    def _publish_to_bus_targets(targets, sender, task, topic_fmt, msg_type):
        """Publish a message to bus for each target. Returns list of results."""
        import urllib.request as _ur
        results = []
        for t in targets:
            msg = {"sender": sender, "topic": topic_fmt(t), "type": msg_type, "content": task}
            try:
                req = _ur.Request(
                    "http://localhost:8420/bus/publish",
                    data=json.dumps(msg).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with _ur.urlopen(req, timeout=3):
                    results.append({"target": t, "topic": topic_fmt(t), "status": "sent"})
            except Exception as e:
                results.append({"target": t, "status": "failed", "error": str(e)})
        return results

    def _validate_worker_target(self, data):
        """Validate target and task from POST data. Returns (targets, task, status_code) or None on error."""
        target = data.get("target", "").strip().lower()
        task = data.get("task", "").strip()
        if not task:
            self._json_response({"error": "task is required"}, status=400)
            return None, None, 400
        if target not in ("alpha", "beta", "gamma", "delta", "all"):
            self._json_response({"error": "target must be alpha|beta|gamma|delta|all"}, status=400)
            return None, None, 400
        targets = ["alpha", "beta", "gamma", "delta"] if target == "all" else [target]
        return targets, task, 200

    def _post_bus_task(self, data):
        targets, task, status = self._validate_worker_target(data)
        if targets is None:
            return status
        sender = data.get("sender", "god-console")
        results = self._publish_to_bus_targets(targets, sender, task, lambda t: f"worker_{t}", "task")
        self._json_response({"ok": True, "method": "bus_task", "dispatched": results, "task": task[:80]})
        return 200

    def _post_dispatch(self, data):
        targets, task, status = self._validate_worker_target(data)
        if targets is None:
            return status
        priority = data.get("priority", "normal")
        msg_type = "urgent-task" if priority == "urgent" else "task"
        results = self._publish_to_bus_targets(targets, "god-console", task, lambda t: t, msg_type)
        self._json_response({"ok": True, "dispatched": results, "task": task[:80], "priority": priority})
        return 200

    def _post_task_create(self, data):
        try:
            from skynet_task_tracker import create_task
            rec = create_task(
                target=data.get("target", "all"), task_text=data.get("task", ""),
                priority=data.get("priority", "normal"), sender=data.get("sender", "god-console"),
                task_id=data.get("task_id"))
            self._json_response({"ok": True, "task": rec})
            return 200
        except ValueError as ve:
            self._json_response({"error": str(ve)}, status=400)
            return 400
        except Exception as e:
            self._json_response({"error": str(e)}, status=500)
            return 500

    def _post_task_update(self, data):
        try:
            from skynet_task_tracker import update_task
            tid = data.get("task_id", "")
            if not tid:
                self._json_response({"error": "task_id required"}, status=400)
                return 400
            rec = update_task(tid, data.get("status"), data.get("result"))
            if rec:
                self._json_response({"ok": True, "task": rec})
                return 200
            self._json_response({"error": f"task {tid} not found"}, status=404)
            return 404
        except ValueError as ve:
            self._json_response({"error": str(ve)}, status=400)
            return 400
        except Exception as e:
            self._json_response({"error": str(e)}, status=500)
            return 500

    def _post_kill_authorize(self, data):
        try:
            sys.path.insert(0, str(Path(__file__).parent / "tools"))
            from skynet_kill_auth import authorize_kill_manual
            rid = data.get("id", data.get("request_id", ""))
            if not rid:
                self._json_response({"error": "request_id required"}, status=400)
                return 400
            ok, msg = authorize_kill_manual(rid)
            self._json_response({"ok": ok, "message": msg})
            return 200
        except Exception as e:
            self._json_response({"error": str(e)}, status=500)
            return 500

    def _post_kill_deny(self, data):
        try:
            sys.path.insert(0, str(Path(__file__).parent / "tools"))
            from skynet_kill_auth import deny_kill_manual
            rid = data.get("id", data.get("request_id", ""))
            reason = data.get("reason", "Orchestrator denied")
            if not rid:
                self._json_response({"error": "request_id required"}, status=400)
                return 400
            ok, msg = deny_kill_manual(rid, reason)
            self._json_response({"ok": ok, "message": msg})
            return 200
        except Exception as e:
            self._json_response({"error": str(e)}, status=500)
            return 500

    def _post_worker_metrics(self, data):
        parts = self.path.split("/")
        worker_name = parts[2] if len(parts) >= 4 else ""
        if worker_name not in ("alpha", "beta", "gamma", "delta"):
            self._json_response({"error": f"unknown worker: {worker_name}"}, status=400)
            return 400
        try:
            sys.path.insert(0, str(Path(__file__).parent / "tools"))
            from skynet_smart_router import record_metrics
            duration_ms = float(data.get("duration_ms", 0))
            outcome = data.get("outcome", "success")
            task_summary = data.get("task", data.get("task_summary", ""))
            result = record_metrics(worker_name, duration_ms, outcome, task_summary)
            self._json_response({"ok": True, "metrics": result})
            return 200
        except Exception as e:
            self._json_response({"error": str(e)}, status=500)
            return 500

    def _post_worker_activity(self, data):
        parts = self.path.split("/")
        worker_name = parts[3] if len(parts) >= 5 else ""
        if worker_name not in ("alpha", "beta", "gamma", "delta"):
            self._json_response({"error": f"unknown worker: {worker_name}"}, status=400)
            return 400
        data["timestamp"] = data.get("timestamp", _time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        data["epoch"] = data.get("epoch", _time.time())
        _activity_cache[worker_name] = data
        try:
            import urllib.request as _ur2
            fwd = json.dumps({"current_task": data.get("current_activity", data.get("doing", ""))}).encode()
            req = _ur2.Request(
                f"http://localhost:8420/worker/{worker_name}/task",
                data=fwd, headers={"Content-Type": "application/json"}, method="POST")
            _ur2.urlopen(req, timeout=2)
        except Exception:
            pass
        self._json_response({"ok": True, "worker": worker_name})
        return 200

    def log_message(self, fmt, *args):
        pass  # silent — we use _log_access instead

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _log_access(self, path, status, ms):
        try:
            ACCESS_LOG.parent.mkdir(parents=True, exist_ok=True)
            # Rotate if > 1MB
            if ACCESS_LOG.exists() and ACCESS_LOG.stat().st_size > 1_000_000:
                old = ACCESS_LOG.with_suffix(".old")
                if old.exists():
                    old.unlink()
                ACCESS_LOG.rename(old)
            ts = _time.strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(ACCESS_LOG, "a", encoding="utf-8") as f:
                f.write(f"{ts} {path} {status} {ms:.1f}ms\n")
        except Exception:
            pass

    def _json_response(self, data, status=200):
        text = _dumps(data)
        body = text.encode() if isinstance(text, str) else text
        del data, text  # free source objects before sending
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        del body


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    address_family = __import__('socket').AF_INET6
    allow_reuse_address = True

    def server_bind(self):
        """Bind to dual-stack (IPv4+IPv6) so 'localhost' resolves instantly."""
        import socket
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def main():
    parser = argparse.ArgumentParser(description="GOD Console -- Skynet Control Center")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", default=True,
                        help="Don't auto-open browser (default: True, kept for backward compat)")
    parser.add_argument("--open", action="store_true",
                        help="Force auto-open browser tab on start")
    args = parser.parse_args()

    # ── PID guard: prevent duplicate GOD Console instances ──
    if not _claim_pid("god-console"):
        return

    print(f"\033[93m GOD Console v3 Level 3\033[0m")
    print(f"   HTTP on http://localhost:{args.port}")
    print(f"   WebSocket on ws://localhost:{WS_PORT}")
    print(f"   Skynet backend: http://localhost:8420")
    print(f"   Access log: {ACCESS_LOG}")
    print(f"   Press Ctrl+C to stop\n")

    # Start WebSocket server for real-time push
    _start_ws_server()

    # Start bus listener for TODO updates
    _start_bus_todo_listener()

    # Start background precomputation for heavy endpoints
    _start_precompute()

    # Bind to localhost only — prevents LAN access without authentication  # signed: beta
    server = ThreadedHTTPServer(("localhost", args.port), ConsoleHandler)

    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{args.port}/dashboard")).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\033[93m⚡ GOD Console offline.\033[0m")
        server.server_close()
    finally:
        _cleanup_pid()


if __name__ == "__main__":
    main()
