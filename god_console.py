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
import os
import sys
import time as _time
import threading
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
_CONSULTANT_PORTS = (8422, 8424)
_BUS_TTL = 2         # bus messages cached 2s
_ENGINES_TTL = 30    # engine probes are expensive; 30s cache
_DASHBOARD_TTL = 3   # combined dashboard data
_WINDOWS_TTL = 5     # window scan cached 5s
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
    # Computation lock prevents stampeding herd — only one thread computes
    with _pulse_compute_lock:
        # Double-check after acquiring lock (another thread may have filled cache)
        with _cache_lock:
            if _cache["pulse"] and (_time.time() - _cache["pulse_t"]) < _PULSE_TTL:
                return _cache["pulse"]
        skynet = _get_skynet_self()
        raw_pulse = skynet.quick_pulse()
        # Engine data comes from quick_pulse's health.pulse() (already cached 30s in engine_metrics)
        try:
            engines_data = _cached_engines()
            engines = engines_data.get("engines", {})
            engines_online = sum(1 for e in engines.values() if e.get("status") == "online")
            engines_total = len(engines)
            engine_names = [e.get("name", k) for k, e in engines.items() if e.get("status") == "online"]
        except Exception:
            engines_online, engines_total, engine_names = 0, 0, []
        agents = raw_pulse.get("agents", {})
        alive = raw_pulse.get("alive", 0)
        total = raw_pulse.get("total", 5)
        workers = {}
        for aid, status in agents.items():
            workers[aid] = {"status": status, "model": "opus-fast"}
        assessment = (
            f"{alive}/{total} workers connected. "
            f"{engines_online}/{engines_total} engines online: {', '.join(engine_names[:6])}. "
            f"Health: {raw_pulse.get('health','UNKNOWN')}. Level 3 intelligence active."
        )
        # IQ already computed in quick_pulse — reuse it
        iq_raw = raw_pulse.get("iq", 0)
        iq = round(iq_raw * 100) if iq_raw <= 1 else round(iq_raw)
        # Lightweight breakdown from available data
        worker_pct = alive / max(1, total)
        engine_pct = engines_online / max(1, engines_total)
        iq_breakdown = {
            "workers": {"score": round(worker_pct * 25, 1), "detail": f"{alive}/{total} alive"},
            "engines": {"score": round(engine_pct * 25, 1), "detail": f"{engines_online}/{engines_total} online"},
            "uptime": {"score": min(10, round((_time.time() - _SERVER_START) / 3600 * 2.5, 1)), "detail": f"{(_time.time() - _SERVER_START)/60:.0f}min"},
            "bus": {"score": 10, "detail": "connected"},
        }
        # Window data already available from quick_pulse -> health.pulse()
        # Avoid redundant get_window_summary() call
        data = {
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
        with _cache_lock:
            _cache["pulse"] = data
            _cache["pulse_t"] = _time.time()
        return data

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
        data = None
        for port in _CONSULTANT_PORTS:
            payload = _fetch_backend(f"http://localhost:{port}/consultants", timeout=2, retries=0)
            consultant = payload.get("consultant") if isinstance(payload, dict) else None
            if not isinstance(consultant, dict):
                continue
            age = consultant.get("heartbeat_age_s")
            stale_after = consultant.get("stale_after_s", 8)
            try:
                age_f = float(age)
                stale_f = float(stale_after)
            except Exception:
                age_f = None
                stale_f = None
            if age_f is not None and stale_f is not None and age_f <= stale_f:
                consultant["live"] = True
                consultant["status"] = "LIVE"
                consultant["pid_alive"] = True
                data = {"consultant": consultant}
                break
            if data is None:
                data = {"consultant": consultant}
        if data is None:
            from skynet_consultant_bridge import get_consultant_view
            consultant = get_consultant_view()
            data = {"consultant": consultant} if consultant else {}
    except Exception as e:
        data = {"error": str(e)}
    with _cache_lock:
        _cache["consultants"] = data
        _cache["consultants_t"] = _time.time()
    return data

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
    for wname, wdata in agents.items():
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
    # Add response_time_ms per engine
    engines = data.get("engines", {})
    per_engine_ms = total_ms / max(1, len(engines))
    for ename, edata in engines.items():
        edata["response_time_ms"] = round(per_engine_ms, 1)
    data["total_probe_ms"] = round(total_ms, 1)
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
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=5)
    data = {
        "status": results.get("status", {}),
        "pulse": results.get("pulse", {}),
        "bus": results.get("bus", []),
        "consultants": results.get("status", {}).get("consultants", {}),
        "errors": errors if errors else None,
        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S"),
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
        except Exception:
            pass
        try:
            _cached_engines()
        except Exception:
            pass
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
        except Exception:
            pass
        # Precompute assess (uses introspect result, avoids double 9s call)
        try:
            now = _time.time()
            with _cache_lock:
                assess_stale = not _cache["assess"] or (now - _cache["assess_t"]) >= _INTROSPECT_TTL
            if assess_stale and reflection:
                sky = _get_skynet_self()
                text = sky._self_assessment(reflection)
                data = {"assessment": text, "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "version": "3.0", "level": 3}
                with _cache_lock:
                    _cache["assess"] = data
                    _cache["assess_t"] = _time.time()
        except Exception:
            pass
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
    """Persist TODOs to disk."""
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
        ts = _time.strftime("%Y-%m-%dT%H:%M:%S")
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
            self._json_response({"error": str(e), "endpoint": self.path}, status=500)
        elapsed_ms = (_time.time() - t0) * 1000
        self._log_access(self.path, status_code, elapsed_ms)

    def _route(self, t0):
        if self.path in ("/", "/index.html", "/god", "/god_console.html"):
            self.send_response(302)
            self.send_header("Location", "/dashboard")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
        elif self.path == "/version":
            self._json_response({"version": "3.0", "level": 3, "codename": "Level 3"})
        elif self.path == "/health":
            endpoints = ["/", "/dashboard", "/engines", "/version", "/health",
                         "/skynet/self/pulse", "/skynet/self/status",
                         "/skynet/self/introspect", "/skynet/self/goals",
                         "/skynet/self/assess", "/skynet/status",
                         "/status", "/god_state", "/bus", "/bus/tasks", "/bus/convene", "/bus/stats",
                         "/windows", "/workers/health", "/dashboard/data", "/ws/info", "/todos", "/consultants",
                         "/processes", "/overseer", "/stream/dashboard",
                         "/kill/pending", "/kill/log", "/learner/health"]
            self._json_response({
                "status": "ok",
                "uptime_s": round(_time.time() - _SERVER_START, 1),
                "endpoints_active": len(endpoints),
                "pid": os.getpid(),
                "ws_port": WS_PORT,
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
                "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S"),
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
                    data = {"assessment": text, "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S"),
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
                data = {"status_line": line, "health": health, "iq": iq}
            except Exception as e:
                data = {"status_line": f"SKYNET v3.0 Level 3 | ERROR: {e}", "health": "ERROR", "iq": 0}
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
            self._json_response(backend)
        elif self.path == "/god_state":
            try:
                pulse = _cached_pulse()
                from skynet_collective import intelligence_score
                iq = intelligence_score()
                health = pulse.get("health", "UNKNOWN")
                iq_score = iq.get("intelligence_score", 0)
                briefing = f"System Health: {health} | Collective IQ: {iq_score:.3f}"
                data = {"briefing": briefing, "health": health, "collective_iq": iq_score}
            except Exception as e:
                data = {"briefing": f"Self-awareness unavailable: {e}", "health": "UNKNOWN", "collective_iq": 0}
            self._json_response(data)
        elif self.path == "/windows":
            try:
                data = _cached_windows()
            except Exception as e:
                data = {"error": str(e)}
            self._json_response(data)
        elif self.path == "/workers/health":
            try:
                from skynet_stuck_detector import get_worker_health_json
                data = get_worker_health_json()
            except Exception as e:
                data = {"error": str(e), "workers": {}}
            self._json_response(data)
        elif self.path == "/learner/health":
            try:
                pid_file = Path(__file__).resolve().parent / "data" / "learner.pid"
                if pid_file.exists():
                    pid = int(pid_file.read_text().strip())
                    import os as _os
                    _os.kill(pid, 0)  # check alive
                    state_file = Path(__file__).resolve().parent / "data" / "learner_state.json"
                    state = json.loads(state_file.read_text()) if state_file.exists() else {}
                    data = {
                        "status": "running",
                        "pid": pid,
                        "episodes_processed": state.get("total_processed", 0),
                        "total_learnings": state.get("total_learnings", 0),
                        "last_run": state.get("last_run"),
                        "started_at": state.get("started_at"),
                    }
                else:
                    data = {"status": "stopped", "pid": None}
            except (OSError, ValueError):
                data = {"status": "stopped", "pid": None}
            except Exception as e:
                data = {"status": "error", "error": str(e)}
            self._json_response(data)
        elif self.path == "/learner/metrics":
            # Learning Telemetry — real data only (Truth Principle)
            try:
                base = Path(__file__).resolve().parent
                metrics = {"timestamp": _time.time()}

                # 1. Episodes from learning_episodes.json
                ep_file = base / "data" / "learning_episodes.json"
                episodes = []
                if ep_file.exists():
                    try:
                        raw = json.loads(ep_file.read_text(encoding="utf-8"))
                        if isinstance(raw, list):
                            episodes = raw
                    except Exception:
                        pass
                metrics["total_episodes"] = len(episodes)

                # Outcome breakdown
                outcomes = {"success": 0, "failure": 0, "unknown": 0}
                for ep in episodes:
                    o = ep.get("outcome", "unknown")
                    if o in outcomes:
                        outcomes[o] += 1
                    else:
                        outcomes["unknown"] += 1
                metrics["by_outcome"] = outcomes

                # Last episode timestamp
                if episodes:
                    last = episodes[-1]
                    metrics["last_episode_ts"] = last.get("timestamp_iso") or last.get("timestamp")
                    metrics["last_episode_worker"] = last.get("worker")
                else:
                    metrics["last_episode_ts"] = None
                    metrics["last_episode_worker"] = None

                # Episode rate buckets (hourly, last 24 entries max)
                rate_buckets = []
                if episodes:
                    now = _time.time()
                    bucket_size = 3600  # 1 hour
                    for ep in episodes:
                        ts = ep.get("timestamp", 0)
                        bucket_idx = int((now - ts) / bucket_size)
                        rate_buckets.append(bucket_idx)
                    from collections import Counter as _Counter
                    bucket_counts = _Counter(rate_buckets)
                    max_bucket = max(bucket_counts.keys()) if bucket_counts else 0
                    sparkline = []
                    for i in range(min(max_bucket + 1, 24)):
                        sparkline.append(bucket_counts.get(i, 0))
                    sparkline.reverse()  # oldest first
                    metrics["sparkline_hourly"] = sparkline
                else:
                    metrics["sparkline_hourly"] = []

                # 2. LearningStore stats (facts from SQLite)
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

                # 3. Learner daemon status
                pid_file = base / "data" / "learner.pid"
                daemon_status = "stopped"
                try:
                    if pid_file.exists():
                        pid = int(pid_file.read_text().strip())
                        import os as _os2
                        _os2.kill(pid, 0)
                        daemon_status = "running"
                except (OSError, ValueError):
                    daemon_status = "stopped"
                metrics["daemon_status"] = daemon_status

                self._json_response(metrics)
            except Exception as e:
                self._json_response({"error": str(e), "total_episodes": 0, "by_outcome": {"success": 0, "failure": 0, "unknown": 0}, "sparkline_hourly": [], "total_facts": 0, "daemon_status": "error", "timestamp": _time.time()}, status=200)
        elif self.path == "/ws/info":
            self._json_response({"ws_url": f"ws://localhost:{WS_PORT}", "protocol": "websocket", "fallback": "polling"})
        elif self.path == "/todos":
            data = _load_todos()
            self._json_response(data)
        elif self.path == "/consultants":
            data = _cached_consultants()
            self._json_response(data)
        elif self.path == "/processes":
            try:
                from skynet_process_guard import _load_registry
                data = _load_registry()
            except Exception as e:
                data = {"error": str(e), "processes": []}
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
                ok, count, tasks = can_stop(worker)
                self._json_response({"can_stop": ok, "pending": count, "tasks": tasks})
            except Exception as e:
                self._json_response({"error": str(e)}, status=500)
        elif self.path == "/kill/pending":
            try:
                sys.path.insert(0, str(Path(__file__).parent / "tools"))
                from skynet_kill_auth import get_pending_requests
                raw = get_pending_requests()
                # Enrich with votes from bus (using cached bus data)
                requests = []
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
                for p in raw:
                    rid = p.get("request_id", "")
                    ts = p.get("timestamp", "")
                    # Convert ISO timestamp to Unix epoch for countdown
                    epoch = 0
                    try:
                        from datetime import datetime as _dt
                        epoch = _dt.fromisoformat(ts).timestamp()
                    except Exception:
                        epoch = _time.time()
                    requests.append({
                        "id": rid,
                        "pid": p.get("pid"),
                        "name": p.get("name", "unknown"),
                        "reason": p.get("reason", ""),
                        "requester": p.get("requester", "?"),
                        "timestamp": epoch,
                        "status": p.get("status", "voting"),
                        "votes": votes_by_req.get(rid, {}),
                    })
                self._json_response({"requests": requests})
            except Exception as e:
                self._json_response({"requests": [], "error": str(e)})
        elif self.path == "/kill/log" or self.path.startswith("/kill/log?"):
            try:
                sys.path.insert(0, str(Path(__file__).parent / "tools"))
                from skynet_kill_auth import get_kill_log
                limit = 20
                if "?" in self.path:
                    import urllib.parse
                    qs = urllib.parse.parse_qs(self.path.split("?", 1)[1])
                    try:
                        limit = int(qs.get("limit", [20])[0])
                    except (ValueError, TypeError):
                        limit = 20
                self._json_response(get_kill_log(limit))
            except Exception as e:
                self._json_response({"error": str(e)}, status=500)
        elif self.path == "/stream/dashboard":
            # SSE endpoint: pushes aggregated dashboard data every 2s
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                while True:
                    payload = {}
                    try:
                        payload["status"] = _cached_backend_status()
                    except Exception:
                        payload["status"] = None
                    try:
                        payload["consultants"] = _cached_consultants()
                    except Exception:
                        payload["consultants"] = {}
                    try:
                        payload["bus"] = _cached_bus(20)
                    except Exception:
                        payload["bus"] = []
                    try:
                        payload["todos"] = _load_todos()
                    except Exception:
                        payload["todos"] = None
                    try:
                        sf = os.path.join(os.path.dirname(__file__), "data", "overseer_status.json")
                        if os.path.exists(sf):
                            payload["overseer"] = json.loads(open(sf, encoding="utf-8").read())
                    except Exception:
                        pass
                    line = f"data: {json.dumps(payload, default=str)}\n\n"
                    self.wfile.write(line.encode())
                    self.wfile.flush()
                    _time.sleep(2)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass  # silent — we use _log_access instead

    def do_POST(self):
        t0 = _time.time()
        status_code = 200
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            data = json.loads(body)
            if self.path == "/todos":
                sender = data.get("sender", "unknown")
                items = data.get("items", [])
                result = _update_worker_todos(sender, items)
                self._json_response({"ok": True, "sender": sender, "total_workers": len(result)})
            elif self.path == "/bus/publish":
                # Proxy POST to backend
                import urllib.request
                req = urllib.request.Request(
                    "http://localhost:8420/bus/publish",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        self._json_response(json.loads(resp.read()))
                except Exception as proxy_err:
                    self._json_response({"error": f"proxy failed: {proxy_err}"}, status=502)
                    status_code = 502
            elif self.path == "/bus/task":
                # Bus-based task delivery: publishes to worker_{target} topic
                # so the worker's local bus_worker daemon picks it up
                target = data.get("target", "").strip().lower()
                task = data.get("task", "").strip()
                sender = data.get("sender", "god-console")
                if not task:
                    self._json_response({"error": "task is required"}, status=400)
                    status_code = 400
                elif target not in ("alpha", "beta", "gamma", "delta", "all"):
                    self._json_response({"error": "target must be alpha|beta|gamma|delta|all"}, status=400)
                    status_code = 400
                else:
                    targets = ["alpha", "beta", "gamma", "delta"] if target == "all" else [target]
                    results = []
                    import urllib.request as _ur
                    for t in targets:
                        msg = {
                            "sender": sender,
                            "topic": f"worker_{t}",
                            "type": "task",
                            "content": task,
                        }
                        try:
                            req = _ur.Request(
                                "http://localhost:8420/bus/publish",
                                data=json.dumps(msg).encode(),
                                headers={"Content-Type": "application/json"},
                                method="POST",
                            )
                            with _ur.urlopen(req, timeout=3) as resp:
                                results.append({"target": t, "topic": f"worker_{t}", "status": "sent"})
                        except Exception as e:
                            results.append({"target": t, "status": "failed", "error": str(e)})
                    self._json_response({"ok": True, "method": "bus_task", "dispatched": results, "task": task[:80]})
            elif self.path == "/dispatch":
                target = data.get("target", "").strip().lower()
                task = data.get("task", "").strip()
                priority = data.get("priority", "normal")
                if not task:
                    self._json_response({"error": "task is required"}, status=400)
                    status_code = 400
                elif target not in ("alpha", "beta", "gamma", "delta", "all"):
                    self._json_response({"error": "target must be alpha|beta|gamma|delta|all"}, status=400)
                    status_code = 400
                else:
                    targets = ["alpha", "beta", "gamma", "delta"] if target == "all" else [target]
                    results = []
                    import urllib.request as _ur
                    for t in targets:
                        msg = {
                            "sender": "god-console",
                            "topic": t,
                            "type": "urgent-task" if priority == "urgent" else "task",
                            "content": task,
                        }
                        try:
                            req = _ur.Request(
                                "http://localhost:8420/bus/publish",
                                data=json.dumps(msg).encode(),
                                headers={"Content-Type": "application/json"},
                                method="POST",
                            )
                            with _ur.urlopen(req, timeout=3) as resp:
                                results.append({"target": t, "status": "sent"})
                        except Exception as e:
                            results.append({"target": t, "status": "failed", "error": str(e)})
                    self._json_response({"ok": True, "dispatched": results, "task": task[:80], "priority": priority})
            elif self.path == "/task/create":
                try:
                    from skynet_task_tracker import create_task
                    rec = create_task(
                        target=data.get("target", "all"),
                        task_text=data.get("task", ""),
                        priority=data.get("priority", "normal"),
                        sender=data.get("sender", "god-console"),
                        task_id=data.get("task_id"),
                    )
                    self._json_response({"ok": True, "task": rec})
                except ValueError as ve:
                    self._json_response({"error": str(ve)}, status=400)
                    status_code = 400
                except Exception as e:
                    self._json_response({"error": str(e)}, status=500)
                    status_code = 500
            elif self.path == "/task/update":
                try:
                    from skynet_task_tracker import update_task
                    tid = data.get("task_id", "")
                    if not tid:
                        self._json_response({"error": "task_id required"}, status=400)
                        status_code = 400
                    else:
                        rec = update_task(tid, data.get("status"), data.get("result"))
                        if rec:
                            self._json_response({"ok": True, "task": rec})
                        else:
                            self._json_response({"error": f"task {tid} not found"}, status=404)
                            status_code = 404
                except ValueError as ve:
                    self._json_response({"error": str(ve)}, status=400)
                    status_code = 400
                except Exception as e:
                    self._json_response({"error": str(e)}, status=500)
                    status_code = 500
            elif self.path == "/kill/authorize":
                try:
                    sys.path.insert(0, str(Path(__file__).parent / "tools"))
                    from skynet_kill_auth import authorize_kill_manual
                    rid = data.get("id", data.get("request_id", ""))
                    if not rid:
                        self._json_response({"error": "request_id required"}, status=400)
                        status_code = 400
                    else:
                        ok, msg = authorize_kill_manual(rid)
                        self._json_response({"ok": ok, "message": msg})
                except Exception as e:
                    self._json_response({"error": str(e)}, status=500)
                    status_code = 500
            elif self.path == "/kill/deny":
                try:
                    sys.path.insert(0, str(Path(__file__).parent / "tools"))
                    from skynet_kill_auth import deny_kill_manual
                    rid = data.get("id", data.get("request_id", ""))
                    reason = data.get("reason", "Orchestrator denied")
                    if not rid:
                        self._json_response({"error": "request_id required"}, status=400)
                        status_code = 400
                    else:
                        ok, msg = deny_kill_manual(rid, reason)
                        self._json_response({"ok": ok, "message": msg})
                except Exception as e:
                    self._json_response({"error": str(e)}, status=500)
                    status_code = 500
            else:
                self._json_response({"error": "not found"}, status=404)
                status_code = 404
        except Exception as e:
            status_code = 500
            self._json_response({"error": str(e)}, status=500)
        self._log_access(f"POST {self.path}", status_code, (_time.time() - t0) * 1000)

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
            ts = _time.strftime("%Y-%m-%dT%H:%M:%S")
            with open(ACCESS_LOG, "a", encoding="utf-8") as f:
                f.write(f"{ts} {path} {status} {ms:.1f}ms\n")
        except Exception:
            pass

    def _json_response(self, data, status=200):
        text = _dumps(data)
        body = text.encode() if isinstance(text, str) else text
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            os.kill(old_pid, 0)
            print(f"[god-console] Already running (PID {old_pid}) -- exiting to prevent duplicate")
            return
        except (OSError, ValueError):
            pass  # Stale PID file -- proceed
    PID_FILE.write_text(str(os.getpid()))

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

    server = ThreadedHTTPServer(("", args.port), ConsoleHandler)

    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{args.port}/dashboard")).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\033[93m⚡ GOD Console offline.\033[0m")
        server.server_close()
    finally:
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
