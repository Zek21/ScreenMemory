"""
Skynet Daemon Health Dashboard — Aggregated health from all daemons.

Merges PID/port liveness (skynet_daemon_status), heartbeat timestamps
(service_heartbeats.json), worker UIA health (worker_health.json),
productivity trends (monitor_health.json), and real-time metrics
(realtime.json) into a single JSON structure consumable by the
GOD Console dashboard.

Usage:
    python tools/skynet_daemon_health_dashboard.py              # Full JSON
    python tools/skynet_daemon_health_dashboard.py --summary    # Summary only
    python tools/skynet_daemon_health_dashboard.py --serve      # HTTP server on port 8426

API (importable):
    from tools.skynet_daemon_health_dashboard import aggregate_health
    health = aggregate_health()  # Returns full health dict

# signed: delta
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Health tier thresholds
# ---------------------------------------------------------------------------
TIER_HEALTHY = "HEALTHY"
TIER_DEGRADED = "DEGRADED"
TIER_CRITICAL = "CRITICAL"

CRITICALITY_WEIGHT = {
    "CATASTROPHIC": 100,
    "HIGH": 10,
    "MODERATE": 3,
    "LOW": 1,
}


# ---------------------------------------------------------------------------
# File loaders (safe — return empty on any error)
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    """Load JSON file, return empty dict on any error."""
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Data source collectors
# ---------------------------------------------------------------------------

def _collect_daemon_status() -> list[dict]:
    """Get PID/port liveness from skynet_daemon_status."""
    try:
        from tools.skynet_daemon_status import check_all_daemons
        return check_all_daemons()
    except Exception as e:
        return [{"error": str(e)}]


def _collect_heartbeats() -> dict:
    """Read service_heartbeats.json for last-seen timestamps."""
    return _load_json(DATA / "service_heartbeats.json")


def _collect_worker_health() -> dict:
    """Read worker_health.json for per-worker UIA-probed health."""
    return _load_json(DATA / "worker_health.json")


def _collect_monitor_health() -> dict:
    """Read monitor_health.json for productivity trends."""
    return _load_json(DATA / "monitor_health.json")


def _collect_realtime() -> dict:
    """Read realtime.json for live bus/task metrics."""
    return _load_json(DATA / "realtime.json")


def _collect_daemon_state() -> dict:
    """Read daemon_state.json for mission/worker model state."""
    return _load_json(DATA / "daemon_state.json")


# ---------------------------------------------------------------------------
# Heartbeat enrichment — merge last_seen from heartbeats into daemon status
# ---------------------------------------------------------------------------

def _enrich_with_heartbeats(daemons: list[dict], heartbeats: dict) -> None:
    """Add last_seen and heartbeat_age_s to each daemon from heartbeats."""
    now = time.time()
    # Build name→heartbeat lookup (heartbeat keys may differ from daemon names)
    hb_map = {}
    for key, val in heartbeats.items():
        if isinstance(val, dict):
            hb_map[key.lower()] = val

    for d in daemons:
        if not isinstance(d, dict) or "name" not in d:
            continue
        name = d["name"].lower()
        # Try exact match, then partial
        hb = hb_map.get(name)
        if not hb:
            for hk, hv in hb_map.items():
                if name in hk or hk in name:
                    hb = hv
                    break
        if hb:
            last_seen = hb.get("last_seen")
            if isinstance(last_seen, (int, float)) and last_seen > 0:
                d["heartbeat_last_seen"] = last_seen
                d["heartbeat_age_s"] = round(now - last_seen, 1)
                d["heartbeat_status"] = hb.get("status", "unknown")
            elif "last_seen_ts" in hb:
                d["heartbeat_last_seen_ts"] = hb["last_seen_ts"]
                d["heartbeat_status"] = hb.get("status", "unknown")


# ---------------------------------------------------------------------------
# Health tier calculation
# ---------------------------------------------------------------------------

def _calculate_tier(daemons: list[dict]) -> str:
    """Determine overall health tier from daemon statuses."""
    dead_weight = 0
    for d in daemons:
        if not isinstance(d, dict):
            continue
        if d.get("disabled"):
            continue
        if not d.get("alive", True):
            crit = d.get("criticality", "LOW")
            dead_weight += CRITICALITY_WEIGHT.get(crit, 1)

    if dead_weight >= 100:
        return TIER_CRITICAL
    if dead_weight >= 10:
        return TIER_DEGRADED
    return TIER_HEALTHY


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------

def _build_summary(daemons: list[dict]) -> dict:
    """Build summary counts from daemon statuses."""
    total = 0
    alive = 0
    dead = 0
    disabled = 0
    by_criticality = {}

    for d in daemons:
        if not isinstance(d, dict) or "name" not in d:
            continue
        total += 1
        crit = d.get("criticality", "UNKNOWN")

        if d.get("disabled"):
            disabled += 1
            continue

        if d.get("alive"):
            alive += 1
        else:
            dead += 1
            by_criticality.setdefault(crit, []).append(d["name"])

    return {
        "total_daemons": total,
        "alive": alive,
        "dead": dead,
        "disabled": disabled,
        "dead_by_criticality": by_criticality,
    }


# ---------------------------------------------------------------------------
# Worker summary
# ---------------------------------------------------------------------------

def _build_worker_summary(worker_health: dict) -> dict:
    """Extract worker summary from worker_health.json."""
    workers = {}
    for name, data in worker_health.items():
        if name == "updated" or not isinstance(data, dict):
            continue
        workers[name] = {
            "alive": data.get("alive", False),
            "visible": data.get("visible", False),
            "model": data.get("model", "unknown"),
            "status": data.get("status", "unknown"),
            "checked_at": data.get("checked_at"),
        }
    return workers


# ---------------------------------------------------------------------------
# Metrics from realtime.json
# ---------------------------------------------------------------------------

def _build_metrics(realtime: dict) -> dict:
    """Extract key metrics from realtime.json."""
    return {
        "tasks_completed": realtime.get("tasks_completed", 0),
        "tasks_dispatched": realtime.get("tasks_dispatched", 0),
        "tasks_failed": realtime.get("tasks_failed", 0),
        "bus_depth": realtime.get("bus_depth", 0),
        "pending_results": realtime.get("pending_results", 0),
        "pending_alerts": realtime.get("pending_alerts", 0),
        "uptime_s": realtime.get("uptime_s", 0),
        "last_update": realtime.get("last_update") or realtime.get("timestamp"),
    }


# ---------------------------------------------------------------------------
# Main aggregation
# ---------------------------------------------------------------------------

def aggregate_health() -> dict:
    """
    Aggregate health from all sources into a single dict.

    Returns:
        {
            "aggregated_at": ISO timestamp,
            "health_tier": "HEALTHY"|"DEGRADED"|"CRITICAL",
            "daemons": [...],
            "summary": {...},
            "workers": {...},
            "metrics": {...},
        }
    """
    # Collect from all sources
    daemons = _collect_daemon_status()
    heartbeats = _collect_heartbeats()
    worker_health = _collect_worker_health()
    realtime = _collect_realtime()

    # Enrich daemon data with heartbeat timestamps
    _enrich_with_heartbeats(daemons, heartbeats)

    # Calculate health tier
    tier = _calculate_tier(daemons)

    # Build summaries
    summary = _build_summary(daemons)
    workers = _build_worker_summary(worker_health)
    metrics = _build_metrics(realtime)

    return {
        "aggregated_at": _iso_now(),
        "health_tier": tier,
        "daemons": daemons,
        "summary": summary,
        "workers": workers,
        "metrics": metrics,
    }


def aggregate_summary() -> dict:
    """Lightweight summary without full daemon details."""
    health = aggregate_health()
    return {
        "aggregated_at": health["aggregated_at"],
        "health_tier": health["health_tier"],
        "summary": health["summary"],
        "workers": health["workers"],
        "metrics": health["metrics"],
    }


# ---------------------------------------------------------------------------
# HTTP server mode
# ---------------------------------------------------------------------------

def _serve(port: int = 8426) -> None:
    """Run a lightweight HTTP server exposing /health endpoint."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health" or self.path == "/":
                data = aggregate_health()
                body = json.dumps(data, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/summary":
                data = aggregate_summary()
                body = json.dumps(data, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass  # Suppress default logging

    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"Daemon health dashboard serving on http://127.0.0.1:{port}")
    print(f"  GET /health  — full aggregated health")
    print(f"  GET /summary — lightweight summary")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Daemon Health Dashboard")
    parser.add_argument("--summary", action="store_true", help="Summary only")
    parser.add_argument("--serve", action="store_true", help="Run HTTP server")
    parser.add_argument("--port", type=int, default=8426, help="Server port")
    args = parser.parse_args()

    if args.serve:
        _serve(args.port)
    elif args.summary:
        print(json.dumps(aggregate_summary(), indent=2))
    else:
        print(json.dumps(aggregate_health(), indent=2))


if __name__ == "__main__":
    main()
