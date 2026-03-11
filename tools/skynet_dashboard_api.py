"""Unified Skynet Dashboard API.

Aggregates missions, performance, observability, and security audit
into a single /api/v1/dashboard endpoint with 5s cache TTL.

Usage:
    python tools/skynet_dashboard_api.py              # smoke test
    python tools/skynet_dashboard_api.py --serve 8430  # run standalone server
"""
import sys, os, json, time, threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
_cache: dict = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 5  # seconds


def _cached(key: str, fn, ttl: int = _CACHE_TTL):
    """Return cached result or call fn() and cache it."""
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
        if entry and now - entry["ts"] < ttl:
            return entry["data"]
    data = fn()
    with _cache_lock:
        _cache[key] = {"data": data, "ts": time.time()}
    return data


# ---------------------------------------------------------------------------
# Data collectors (each returns a dict, never raises)
# ---------------------------------------------------------------------------

def _collect_missions() -> dict:
    """Mission summary from MissionControl."""
    try:
        from tools.skynet_missions import MissionControl, MissionStatus
        mc = MissionControl()
        all_m = mc.list_missions(limit=200)
        by_status: dict[str, int] = {}
        for m in all_m:
            s = m.status if isinstance(m.status, str) else m.status.value
            by_status[s] = by_status.get(s, 0) + 1
        recent = []
        for m in all_m[:10]:
            recent.append({
                "id": m.id,
                "title": m.title,
                "status": m.status if isinstance(m.status, str) else m.status.value,
                "owner": m.owner,
                "priority": m.priority,
            })
        return {"total": len(all_m), "by_status": by_status, "recent": recent}
    except Exception as e:
        return {"error": str(e)}


def _collect_performance() -> dict:
    """Worker performance leaderboard from SmartRouter."""
    try:
        from tools.skynet_smart_router import get_leaderboard
        return {"leaderboard": get_leaderboard()}
    except Exception as e:
        return {"error": str(e)}


def _collect_observability() -> dict:
    """System health + throughput from observability module."""
    try:
        from tools.skynet_observability import system_health, throughput_metrics
        return {
            "health": system_health(),
            "throughput": throughput_metrics(),
        }
    except Exception as e:
        return {"error": str(e)}


def _collect_security() -> dict:
    """Security audit summary."""
    try:
        from tools.skynet_security_audit import full_audit
        result = full_audit(auto_fix=False)
        if isinstance(result, dict):
            return result
        # AuditResult dataclass
        return {
            "passed": result.passed,
            "failed": result.failed,
            "warnings": result.warnings,
            "critical": result.critical,
            "details": result.details[:20],  # cap detail lines
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Aggregated endpoint
# ---------------------------------------------------------------------------

def collect_dashboard() -> dict:
    """Aggregate all four data sources into one response."""
    return _cached("dashboard", lambda: {
        "timestamp": time.time(),
        "missions": _collect_missions(),
        "performance": _collect_performance(),
        "observability": _collect_observability(),
        "security": _collect_security(),
        "cache_ttl_s": _CACHE_TTL,
    })


# ---------------------------------------------------------------------------
# Lightweight HTTP server (optional standalone mode)
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/v1/dashboard":
            data = collect_dashboard()
            body = json.dumps(data, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass  # suppress request logs


def serve(port: int = 8430):
    """Run standalone HTTP server."""
    server = HTTPServer(("127.0.0.1", port), _Handler)
    print(f"Dashboard API serving on http://127.0.0.1:{port}/api/v1/dashboard")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test():
    """Quick validation that collect_dashboard returns well-formed data."""
    print("Running smoke test...")
    data = collect_dashboard()
    assert isinstance(data, dict), "Expected dict"
    for key in ("timestamp", "missions", "performance", "observability", "security", "cache_ttl_s"):
        assert key in data, f"Missing key: {key}"
    assert isinstance(data["timestamp"], float), "timestamp should be float"
    assert data["cache_ttl_s"] == _CACHE_TTL, "cache_ttl_s mismatch"

    # Verify cache works (second call within TTL should be instant)
    t0 = time.time()
    data2 = collect_dashboard()
    elapsed = time.time() - t0
    assert data2["timestamp"] == data["timestamp"], "Cache miss -- timestamps differ"
    assert elapsed < 0.1, f"Cache took too long: {elapsed:.3f}s"

    print(f"  timestamp : {data['timestamp']}")
    print(f"  missions  : {list(data['missions'].keys())}")
    print(f"  performance: {list(data['performance'].keys())}")
    print(f"  observability: {list(data['observability'].keys())}")
    print(f"  security  : {list(data['security'].keys())}")
    print(f"  cache_ttl : {data['cache_ttl_s']}s")
    print("Smoke test PASSED")


if __name__ == "__main__":
    if "--serve" in sys.argv:
        idx = sys.argv.index("--serve")
        port = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 8430
        serve(port)
    else:
        _smoke_test()
