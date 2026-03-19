"""Unified Worker Status -- Merged view of core + external workers.

Combines core worker states (from workers.json + UIA engine) with
external worker states (from external_workers.json + health data)
into a single status table.

CLI:
    python tools/skynet_unified_status.py              # Full table
    python tools/skynet_unified_status.py --json       # Machine-readable JSON
    python tools/skynet_unified_status.py --brief      # Compact one-liner per worker

Output format:
    Worker          | Type     | State      | Model OK | Last Activity
    alpha           | core     | IDLE       | True     | 2m ago
    beta            | core     | PROCESSING | True     | now
    website-worker  | external | IDLE       | True     | 5m ago
"""
# signed: beta

import argparse
import ctypes
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _REPO_ROOT / "data"
_WORKERS_FILE = _DATA_DIR / "workers.json"
_EXT_WORKERS_FILE = _DATA_DIR / "external_workers.json"
_EXT_HEALTH_FILE = _DATA_DIR / "external_worker_health.json"

# Ensure parent module is importable
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ============================================================================ #
#                              DATA LOADERS                                    #
# ============================================================================ #
# signed: beta


def _load_json(path: Path) -> Any:
    """Safely load a JSON file, returning None on failure."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _time_ago(iso_ts: Optional[str]) -> str:
    """Convert ISO timestamp to human-readable relative time."""
    # signed: beta
    if not iso_ts:
        return "never"
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta_s = (datetime.now(timezone.utc) - dt).total_seconds()
        if delta_s < 0:
            return "future"
        if delta_s < 10:
            return "now"
        if delta_s < 60:
            return f"{int(delta_s)}s ago"
        if delta_s < 3600:
            return f"{int(delta_s / 60)}m ago"
        if delta_s < 86400:
            return f"{int(delta_s / 3600)}h ago"
        return f"{int(delta_s / 86400)}d ago"
    except Exception:
        return "unknown"


def _check_hwnd_alive(hwnd: int) -> bool:
    """Quick Win32 IsWindow check."""
    try:
        return bool(ctypes.windll.user32.IsWindow(hwnd))
    except Exception:
        return False


# ============================================================================ #
#                           CORE WORKER STATUS                                 #
# ============================================================================ #
# signed: beta


def _get_core_worker_states() -> List[Dict[str, Any]]:
    """Get status for all core workers from workers.json + UIA scan."""
    # signed: beta
    raw = _load_json(_WORKERS_FILE)
    if raw is None:
        return []

    workers_list = raw.get("workers", []) if isinstance(raw, dict) else raw

    # Try UIA engine for live state
    uia_available = False
    engine = None
    try:
        from tools.uia_engine import get_engine
        engine = get_engine()
        uia_available = True
    except Exception:
        pass

    results = []
    for w in workers_list:
        name = w.get("name", "unknown")
        hwnd = w.get("hwnd", 0)
        alive = _check_hwnd_alive(hwnd) if hwnd else False

        entry: Dict[str, Any] = {
            "name": name,
            "type": "core",
            "hwnd": hwnd,
            "alive": alive,
            "state": "DEAD" if not alive else "UNKNOWN",
            "model": w.get("model", ""),
            "model_ok": False,
            "agent_ok": False,
            "last_activity": None,
        }

        # UIA scan for live state
        if alive and uia_available and engine and hwnd:
            try:
                scan = engine.scan(hwnd)
                entry["state"] = scan.state
                entry["model"] = scan.model or entry["model"]
                entry["model_ok"] = scan.model_ok
                entry["agent_ok"] = scan.agent_ok
            except Exception:
                entry["state"] = "SCAN_FAIL"

        results.append(entry)

    # Try to get last activity from backend /status
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://localhost:8420/status", method="GET"
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            status_data = json.loads(resp.read().decode("utf-8"))
            agents = status_data.get("agents", [])
            for agent in agents:
                agent_name = agent.get("name", "")
                for entry in results:
                    if entry["name"] == agent_name:
                        last_hb = agent.get("last_heartbeat")
                        if last_hb and last_hb != "0001-01-01T00:00:00Z":
                            entry["last_activity"] = last_hb
                        break
    except Exception:
        pass

    return results


# ============================================================================ #
#                         EXTERNAL WORKER STATUS                               #
# ============================================================================ #
# signed: beta


def _get_external_worker_states() -> List[Dict[str, Any]]:
    """Get status for all external workers from registry + health data."""
    # signed: beta
    registry = _load_json(_EXT_WORKERS_FILE)
    health_data = _load_json(_EXT_HEALTH_FILE)

    reg_workers = registry.get("workers", {}) if registry else {}
    health_workers = health_data.get("workers", {}) if health_data else {}

    # Merge IDs from both sources
    all_ids = set(reg_workers.keys()) | set(health_workers.keys())

    results = []
    for wid in sorted(all_ids):
        reg = reg_workers.get(wid, {})
        hw = health_workers.get(wid, {})

        hwnd = hw.get("hwnd") or reg.get("hwnd")
        alive = _check_hwnd_alive(int(hwnd)) if hwnd else False

        entry: Dict[str, Any] = {
            "name": wid,
            "type": "external",
            "hwnd": hwnd,
            "alive": alive if hwnd else None,
            "state": "UNKNOWN",
            "model": "",
            "model_ok": False,
            "agent_ok": False,
            "last_activity": reg.get("last_seen"),
            "registry_status": reg.get("status", "unknown"),
        }

        # UIA scan if HWND is alive
        if alive and hwnd:
            try:
                from tools.uia_engine import get_engine
                engine = get_engine()
                scan = engine.scan(int(hwnd))
                entry["state"] = scan.state
                entry["model"] = scan.model or ""
                entry["model_ok"] = scan.model_ok
                entry["agent_ok"] = scan.agent_ok
            except Exception:
                entry["state"] = "SCAN_FAIL"
        elif hwnd and not alive:
            entry["state"] = "DEAD"

        # Heartbeat info
        last_hb = hw.get("last_heartbeat")
        if last_hb:
            entry["last_activity"] = last_hb  # prefer heartbeat over registry

        # Quarantine stats
        try:
            from tools.skynet_external_quarantine import QuarantineStore
            store = QuarantineStore()
            counts = {"PENDING": 0, "APPROVED": 0, "REJECTED": 0}
            for qe in store._entries.values():
                if qe.worker_id == wid and qe.status in counts:
                    counts[qe.status] += 1
            entry["quarantine"] = counts
        except Exception:
            entry["quarantine"] = {}

        # Site health
        site_url = hw.get("site_url") or reg.get("site_url")
        if site_url:
            entry["site_url"] = site_url

        results.append(entry)

    return results


# ============================================================================ #
#                            UNIFIED STATUS                                    #
# ============================================================================ #
# signed: beta


def get_unified_status() -> Dict[str, Any]:
    """Get unified status for ALL workers (core + external).

    Returns dict with:
        workers: list of worker status dicts
        summary: counts by type and state
        timestamp: ISO timestamp of this report
    """
    # signed: beta
    core = _get_core_worker_states()
    external = _get_external_worker_states()

    all_workers = core + external

    # Summary counts
    summary = {
        "core_total": len(core),
        "core_alive": sum(1 for w in core if w.get("alive")),
        "external_total": len(external),
        "external_alive": sum(1 for w in external if w.get("alive")),
        "total": len(all_workers),
        "states": {},
    }
    for w in all_workers:
        st = w.get("state", "UNKNOWN")
        summary["states"][st] = summary["states"].get(st, 0) + 1

    return {
        "workers": all_workers,
        "summary": summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def print_unified_table(brief: bool = False) -> None:
    """Print formatted unified status table to stdout."""
    # signed: beta
    status = get_unified_status()
    workers = status["workers"]

    if not workers:
        print("No workers found.")
        return

    if brief:
        for w in workers:
            sym = "+" if w.get("alive") else "-" if w.get("alive") is False else "?"
            print(f"[{sym}] {w['name']:<18s} {w['type']:<9s} {w['state']:<12s}")
        return

    # Header
    print(f"\n{'='*78}")
    print(f"  Unified Worker Status — {status['timestamp'][:19]}")
    print(f"{'='*78}")
    print()
    print(f"  {'Worker':<18s} {'Type':<10s} {'State':<12s} "
          f"{'Model OK':<10s} {'Last Activity':<15s}")
    print(f"  {'-'*18} {'-'*10} {'-'*12} {'-'*10} {'-'*15}")

    for w in workers:
        name = w["name"]
        wtype = w["type"]
        state = w.get("state", "UNKNOWN")
        model_ok = str(w.get("model_ok", "N/A"))
        last = _time_ago(w.get("last_activity"))

        # Quarantine indicator for external workers
        q = w.get("quarantine", {})
        q_pending = q.get("PENDING", 0)
        q_suffix = f" [Q:{q_pending}p]" if q_pending else ""

        print(f"  {name:<18s} {wtype:<10s} {state:<12s} "
              f"{model_ok:<10s} {last:<15s}{q_suffix}")

    # Summary
    s = status["summary"]
    print()
    print(f"  Summary: {s['core_alive']}/{s['core_total']} core alive, "
          f"{s['external_alive']}/{s['external_total']} external alive, "
          f"{s['total']} total")

    state_parts = [f"{st}={cnt}" for st, cnt in sorted(s["states"].items())]
    if state_parts:
        print(f"  States:  {', '.join(state_parts)}")
    print()


# ============================================================================ #
#                                  CLI                                         #
# ============================================================================ #
# signed: beta


def _cli() -> None:
    """Command-line interface for unified worker status."""
    parser = argparse.ArgumentParser(
        description="Unified Worker Status — Core + External Workers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s                  Full formatted table
  %(prog)s --json           Machine-readable JSON output
  %(prog)s --brief          Compact one-liner per worker
""",
    )
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--brief", action="store_true",
                        help="Compact output (one line per worker)")

    args = parser.parse_args()

    if args.json:
        status = get_unified_status()
        print(json.dumps(status, indent=2, default=str))
    else:
        print_unified_table(brief=args.brief)


if __name__ == "__main__":
    _cli()
