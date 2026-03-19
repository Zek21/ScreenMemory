#!/usr/bin/env python3
"""External Worker Monitor with Quarantine Integration and Health System.

Monitors, dispatches to, and manages results from non-core ("external")
workers that operate outside the Skynet HWND-managed grid.  All external
worker results pass through the quarantine system before being trusted.

Core capabilities:
    status   -- show all known external workers and their state
    scan     -- discover external workers via bus announcements
    dispatch -- send a task to an external worker via bus
    list     -- list recent external worker bus messages
    health   -- full health report for all external workers

Quarantine-integrated capabilities:
    dispatch-q   -- dispatch with automatic quarantine tracking
    monitor      -- watch bus for external results, auto-quarantine
    quarantine   -- show quarantine entries for external workers
    validate     -- trigger cross-validation on a quarantined entry

Health monitoring:
    - HWND alive status (Win32 IsWindow check)
    - UIA state (IDLE/PROCESSING/TYPING via uia_engine)
    - Model correctness (via uia_engine.scan())
    - Heartbeat tracking (bus heartbeats every 60s, alert if missing > 180s)
    - WordPress site health (HTTP check for managed sites)
    - Quarantine stats per worker

CLI:
    python tools/skynet_external_monitor.py status
    python tools/skynet_external_monitor.py scan
    python tools/skynet_external_monitor.py dispatch <worker_id> <task>
    python tools/skynet_external_monitor.py list [--limit N]
    python tools/skynet_external_monitor.py health
    python tools/skynet_external_monitor.py dispatch-q <worker_id> <task>
    python tools/skynet_external_monitor.py monitor [--timeout N]
    python tools/skynet_external_monitor.py quarantine
    python tools/skynet_external_monitor.py validate <quarantine_id>
"""
# signed: gamma
# health system upgrade signed: beta

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# --- paths ------------------------------------------------------------------ #
_TOOLS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _TOOLS_DIR.parent
_DATA_DIR = _ROOT_DIR / "data"
_REGISTRY_FILE = _DATA_DIR / "external_workers.json"
_DISPATCH_LOG = _DATA_DIR / "external_dispatch_log.json"

sys.path.insert(0, str(_ROOT_DIR))

CORE_WORKERS = frozenset({"alpha", "beta", "gamma", "delta"})
BUS_URL = "http://localhost:8420"
HEARTBEAT_INTERVAL_S = 60
HEARTBEAT_STALE_S = 180
_HEALTH_FILE = _DATA_DIR / "external_worker_health.json"

# --- optional imports (graceful degradation) -------------------------------- #
# QuarantineStore is required for quarantine features
try:
    from tools.skynet_external_quarantine import QuarantineStore  # type: ignore
    _HAS_QUARANTINE = True
except ImportError:
    _HAS_QUARANTINE = False
    QuarantineStore = None  # type: ignore[assignment,misc]

# CrossValidator is being built by Beta — may not exist yet
try:
    from tools.skynet_cross_validate import CrossValidator  # type: ignore
    _HAS_CROSS_VALIDATOR = True
except ImportError:
    _HAS_CROSS_VALIDATOR = False
    CrossValidator = None  # type: ignore[assignment,misc]


# ============================================================================ #
#                          EXTERNAL WORKER REGISTRY                            #
# ============================================================================ #

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_registry() -> Dict[str, Any]:
    """Load the external worker registry from disk."""
    if _REGISTRY_FILE.exists():
        try:
            return json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"workers": {}, "updated_at": _now_iso()}


def _save_registry(data: Dict[str, Any]) -> None:
    """Save the external worker registry atomically."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now_iso()
    tmp = _REGISTRY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(_REGISTRY_FILE)


def _load_dispatch_log() -> List[Dict[str, Any]]:
    """Load the external dispatch log."""
    if _DISPATCH_LOG.exists():
        try:
            return json.loads(_DISPATCH_LOG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_dispatch_log(entries: List[Dict[str, Any]]) -> None:
    """Save the external dispatch log (keep last 500 entries)."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    entries = entries[-500:]
    tmp = _DISPATCH_LOG.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    tmp.replace(_DISPATCH_LOG)


def _bus_get(endpoint: str, params: Optional[Dict] = None) -> Optional[Any]:
    """GET request to bus backend."""
    import urllib.request
    import urllib.parse
    url = f"{BUS_URL}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _bus_publish(msg: Dict[str, str]) -> bool:
    """Publish to bus via guarded_publish (mandatory)."""
    try:
        from tools.skynet_spam_guard import guarded_publish  # type: ignore
        result = guarded_publish(msg)
        return result.get("published", False) if isinstance(result, dict) else False
    except Exception:
        return False


# ============================================================================ #
#                        EXISTING CORE FUNCTIONALITY                           #
# ============================================================================ #

def cmd_status() -> None:
    """Show all known external workers and their state."""  # signed: gamma
    registry = _load_registry()
    workers = registry.get("workers", {})
    if not workers:
        print("No external workers registered.")
        print("Run 'scan' to discover workers from bus announcements.")
        return

    print(f"External Workers ({len(workers)}):")
    print(f"{'ID':<20s} {'Status':<12s} {'Last Seen':<25s} {'Tasks':<6s} Source")
    print("-" * 80)
    for wid, info in sorted(workers.items()):
        status = info.get("status", "unknown")
        last_seen = info.get("last_seen", "never")
        task_count = info.get("task_count", 0)
        source = info.get("source", "bus")
        print(f"{wid:<20s} {status:<12s} {last_seen:<25s} {task_count:<6d} {source}")


def cmd_scan() -> None:
    """Discover external workers from bus announcements."""  # signed: gamma
    msgs = _bus_get("/bus/messages", {"limit": 100})
    if not msgs:
        print("Could not read bus messages (is Skynet backend running?).")
        return

    if isinstance(msgs, dict):
        msgs = msgs.get("messages", [])

    registry = _load_registry()
    workers = registry.get("workers", {})
    discovered = 0

    for msg in msgs:
        sender = msg.get("sender", "")
        if sender and sender not in CORE_WORKERS and sender != "orchestrator":
            # Skip system senders
            if sender in ("quarantine_system", "system", "monitor", "external_monitor"):
                continue
            if sender not in workers:
                workers[sender] = {
                    "status": "discovered",
                    "first_seen": _now_iso(),
                    "last_seen": _now_iso(),
                    "task_count": 0,
                    "source": "bus_scan",
                }
                discovered += 1
            else:
                workers[sender]["last_seen"] = _now_iso()

    registry["workers"] = workers
    _save_registry(registry)
    print(f"Scan complete. Discovered {discovered} new external workers. "
          f"Total: {len(workers)}")


def cmd_dispatch(worker_id: str, task: str) -> bool:
    """Send a task to an external worker via bus."""  # signed: gamma
    success = _bus_publish({
        "sender": "external_monitor",
        "topic": "workers",
        "type": "directive",
        "content": json.dumps({
            "target": worker_id,
            "task": task,
            "dispatched_at": _now_iso(),
        }),
    })

    if success:
        # Update registry
        registry = _load_registry()
        workers = registry.get("workers", {})
        if worker_id not in workers:
            workers[worker_id] = {
                "status": "tasked",
                "first_seen": _now_iso(),
                "last_seen": _now_iso(),
                "task_count": 1,
                "source": "direct_dispatch",
            }
        else:
            workers[worker_id]["status"] = "tasked"
            workers[worker_id]["task_count"] = workers[worker_id].get("task_count", 0) + 1
            workers[worker_id]["last_seen"] = _now_iso()
        registry["workers"] = workers
        _save_registry(registry)

        # Log dispatch
        log = _load_dispatch_log()
        log.append({
            "worker_id": worker_id,
            "task": task,
            "dispatched_at": _now_iso(),
            "quarantine_tracked": False,
            "quarantine_id": None,
            "result_received": False,
        })
        _save_dispatch_log(log)
        print(f"Task dispatched to {worker_id}")
    else:
        print(f"Failed to dispatch task to {worker_id} (bus publish failed).")
    return success


def cmd_list(limit: int = 20) -> None:
    """List recent external worker bus messages."""  # signed: gamma
    msgs = _bus_get("/bus/messages", {"limit": limit})
    if not msgs:
        print("Could not read bus messages.")
        return

    if isinstance(msgs, dict):
        msgs = msgs.get("messages", [])

    ext_msgs = [m for m in msgs
                if m.get("sender", "") not in CORE_WORKERS
                and m.get("sender", "") not in ("orchestrator", "system",
                                                 "quarantine_system", "monitor",
                                                 "external_monitor")]
    if not ext_msgs:
        print("No recent external worker messages found.")
        return

    print(f"External worker messages (last {limit}):")
    for msg in ext_msgs:
        sender = msg.get("sender", "?")
        mtype = msg.get("type", "?")
        content = msg.get("content", "")
        ts = msg.get("timestamp", "")
        # Truncate content for display
        content_short = content[:80] + "..." if len(content) > 80 else content
        print(f"  [{ts[:19]}] {sender} ({mtype}): {content_short}")


# ============================================================================ #
#                    QUARANTINE-INTEGRATED CAPABILITIES                         #
# ============================================================================ #

def dispatch_with_quarantine(worker_id: str, task: str) -> Optional[str]:
    """Dispatch task to external worker AND track for quarantine.

    Returns the dispatch log entry ID or None on failure.
    When the worker reports back, monitor_results() will auto-submit
    the result to quarantine.
    """  # signed: gamma
    if not _HAS_QUARANTINE:
        print("ERROR: QuarantineStore not available. "
              "Install tools/skynet_external_quarantine.py first.")
        return None

    success = _bus_publish({
        "sender": "external_monitor",
        "topic": "workers",
        "type": "directive",
        "content": json.dumps({
            "target": worker_id,
            "task": task,
            "dispatched_at": _now_iso(),
            "quarantine_tracked": True,
        }),
    })

    if not success:
        print(f"Failed to dispatch quarantine-tracked task to {worker_id}.")
        return None

    # Update registry
    registry = _load_registry()
    workers = registry.get("workers", {})
    if worker_id not in workers:
        workers[worker_id] = {
            "status": "tasked",
            "first_seen": _now_iso(),
            "last_seen": _now_iso(),
            "task_count": 1,
            "source": "dispatch_q",
        }
    else:
        workers[worker_id]["status"] = "tasked"
        workers[worker_id]["task_count"] = workers[worker_id].get("task_count", 0) + 1
        workers[worker_id]["last_seen"] = _now_iso()
    registry["workers"] = workers
    _save_registry(registry)

    # Log dispatch with quarantine tracking
    log = _load_dispatch_log()
    dispatch_entry = {
        "worker_id": worker_id,
        "task": task,
        "dispatched_at": _now_iso(),
        "quarantine_tracked": True,
        "quarantine_id": None,  # filled when result arrives
        "result_received": False,
    }
    log.append(dispatch_entry)
    _save_dispatch_log(log)

    print(f"Task dispatched to {worker_id} (quarantine-tracked)")
    print(f"  Results will be auto-quarantined when {worker_id} reports back.")
    return worker_id


def monitor_results(timeout: int = 60, poll_interval: float = 3.0) -> List[str]:
    """Watch bus for external worker results and auto-submit to quarantine.

    Polls the bus for messages from non-core workers with type='result'.
    Each result is submitted to QuarantineStore and optionally triggers
    cross-validation.

    Args:
        timeout: Seconds to watch before returning (0 = one-shot scan).
        poll_interval: Seconds between bus polls.

    Returns:
        List of quarantine entry IDs created.
    """  # signed: gamma
    if not _HAS_QUARANTINE:
        print("ERROR: QuarantineStore not available.")
        return []

    store = QuarantineStore()
    quarantine_ids: List[str] = []
    seen_fingerprints: set = set()
    start = time.monotonic()

    print(f"Monitoring bus for external worker results "
          f"(timeout={timeout}s, poll={poll_interval}s)...")

    while True:
        elapsed = time.monotonic() - start
        if timeout > 0 and elapsed >= timeout:
            break

        msgs = _bus_get("/bus/messages", {"limit": 50})
        if msgs is None:
            time.sleep(poll_interval)
            continue

        if isinstance(msgs, dict):
            msgs = msgs.get("messages", [])

        for msg in msgs:
            sender = msg.get("sender", "")
            msg_type = msg.get("type", "")
            content = msg.get("content", "")
            ts = msg.get("timestamp", "")

            # Only process results from non-core, non-system senders
            if sender in CORE_WORKERS or sender in ("orchestrator", "system",
                                                      "quarantine_system",
                                                      "monitor", "external_monitor"):
                continue
            if msg_type != "result":
                continue

            # Deduplicate within this monitoring session
            fingerprint = f"{sender}:{ts}:{content[:100]}"
            if fingerprint in seen_fingerprints:
                continue
            seen_fingerprints.add(fingerprint)

            # Find matching dispatch entry to get task description
            task_desc = _find_dispatch_task(sender)

            # Submit to quarantine
            try:
                qid = store.submit(
                    worker_id=sender,
                    task=task_desc or f"(auto-captured from {sender})",
                    result=content,
                    expiry_minutes=30,
                )
                quarantine_ids.append(qid)
                print(f"  QUARANTINED: {qid} from {sender}")

                # Update dispatch log
                _mark_result_received(sender, qid)

                # Auto-trigger cross-validation request
                _request_cross_validation(qid, sender, content)

            except ValueError as exc:
                # Core worker or other validation error
                print(f"  SKIP: {sender} -- {exc}")

        if timeout == 0:
            break
        time.sleep(poll_interval)

    print(f"\nMonitoring complete. {len(quarantine_ids)} results quarantined.")
    return quarantine_ids


def quarantine_status() -> None:
    """Show quarantine entries for external workers."""  # signed: gamma
    if not _HAS_QUARANTINE:
        print("ERROR: QuarantineStore not available.")
        return

    store = QuarantineStore()
    stats = store.stats()

    print("External Worker Quarantine Status:")
    print(f"  PENDING:    {stats.get('PENDING', 0)}")
    print(f"  VALIDATING: {stats.get('VALIDATING', 0)}")
    print(f"  APPROVED:   {stats.get('APPROVED', 0)}")
    print(f"  REJECTED:   {stats.get('REJECTED', 0)}")
    print(f"  EXPIRED:    {stats.get('EXPIRED', 0)}")
    print(f"  TOTAL:      {stats.get('total', 0)}")

    # Show pending entries with detail
    pending = store.get_pending()
    if pending:
        print(f"\nPending entries ({len(pending)}):")
        for entry in pending:
            age_s = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(entry.submitted_at)).total_seconds()
            remaining_s = max(0, entry.expiry_minutes * 60 - age_s)
            print(f"  {entry.id}")
            print(f"    Worker:    {entry.worker_id}")
            print(f"    Task:      {entry.task_description[:70]}")
            print(f"    Age:       {int(age_s)}s (expires in {int(remaining_s)}s)")
            print(f"    Result:    {entry.result_content[:80]}...")
    else:
        print("\nNo pending entries.")


def validate_result(quarantine_id: str) -> bool:
    """Trigger cross-validation for a quarantined entry.

    If CrossValidator is available (Beta's module), dispatches a
    cross-validation task to an idle core worker.  Otherwise falls
    back to posting a validation request on the bus.

    Returns True if validation was successfully triggered.
    """  # signed: gamma
    if not _HAS_QUARANTINE:
        print("ERROR: QuarantineStore not available.")
        return False

    store = QuarantineStore()
    entry = store.get_entry(quarantine_id)
    if entry is None:
        print(f"ERROR: Quarantine entry {quarantine_id} not found.")
        return False

    if entry.is_terminal():
        print(f"Entry {quarantine_id} is already {entry.status}. No action needed.")
        return False

    # Attempt CrossValidator (Beta's module) first
    if _HAS_CROSS_VALIDATOR and CrossValidator is not None:
        try:
            validator = CrossValidator()
            result = validator.validate(
                entry_id=quarantine_id,
                worker_id=entry.worker_id,
                task=entry.task_description,
                result_content=entry.result_content,
            )
            print(f"Cross-validation triggered via CrossValidator: {result}")
            return True
        except Exception as exc:
            print(f"CrossValidator failed ({exc}), falling back to bus request.")

    # Fallback: post validation request to bus for a core worker to pick up
    success = _bus_publish({
        "sender": "external_monitor",
        "topic": "workers",
        "type": "cross_validation_request",
        "content": json.dumps({
            "quarantine_id": quarantine_id,
            "external_worker": entry.worker_id,
            "task": entry.task_description,
            "result_preview": entry.result_content[:200],
            "requested_at": _now_iso(),
        }),
    })

    if success:
        print(f"Cross-validation request posted to bus for {quarantine_id}.")
        print(f"  A core worker should pick this up and approve/reject.")
        return True
    else:
        print(f"Failed to post cross-validation request for {quarantine_id}.")
        return False


# ============================================================================ #
#                        HEALTH MONITORING SYSTEM                              #
# ============================================================================ #
# signed: beta


def _load_health_data() -> Dict[str, Any]:
    """Load persisted health state for external workers."""
    # signed: beta
    if _HEALTH_FILE.exists():
        try:
            return json.loads(_HEALTH_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"workers": {}, "updated_at": _now_iso()}


def _save_health_data(data: Dict[str, Any]) -> None:
    """Save health state atomically."""
    # signed: beta
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now_iso()
    tmp = _HEALTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(_HEALTH_FILE)


def _check_hwnd_alive(hwnd: int) -> Dict[str, Any]:
    """Check if a window handle is alive and visible via Win32 IsWindow."""
    # signed: beta
    try:
        user32 = ctypes.windll.user32
        is_window = bool(user32.IsWindow(hwnd))
        is_visible = bool(user32.IsWindowVisible(hwnd)) if is_window else False
        return {"alive": is_window, "visible": is_visible}
    except Exception as exc:
        return {"alive": False, "visible": False, "error": str(exc)}


def _check_uia_state(hwnd: int) -> Dict[str, Any]:
    """Check UIA state and model correctness for a window."""
    # signed: beta
    try:
        from tools.uia_engine import get_engine
        engine = get_engine()
        scan_result = engine.scan(hwnd)
        return {
            "state": scan_result.state,
            "model": scan_result.model,
            "agent": scan_result.agent,
            "model_ok": scan_result.model_ok,
            "agent_ok": scan_result.agent_ok,
            "scan_ms": scan_result.scan_ms,
        }
    except Exception as exc:
        return {
            "state": "UNKNOWN",
            "model": "",
            "agent": "",
            "model_ok": False,
            "agent_ok": False,
            "error": str(exc),
        }


def _check_site_health(url: str, timeout: float = 5.0) -> Dict[str, Any]:
    """HTTP health check for a managed website."""
    # signed: beta
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "SkynetHealthCheck/1.0")
        start = time.monotonic()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed_ms = (time.monotonic() - start) * 1000
            return {
                "reachable": True,
                "status_code": resp.status,
                "response_ms": round(elapsed_ms, 1),
            }
    except Exception as exc:
        return {
            "reachable": False,
            "status_code": None,
            "response_ms": None,
            "error": str(exc),
        }


def _get_quarantine_stats_for_worker(worker_id: str) -> Dict[str, int]:
    """Get quarantine entry counts for a specific external worker."""
    # signed: beta
    if not _HAS_QUARANTINE:
        return {}
    try:
        store = QuarantineStore()
        counts = {"PENDING": 0, "VALIDATING": 0, "APPROVED": 0,
                  "REJECTED": 0, "EXPIRED": 0}
        for entry in store._entries.values():
            if entry.worker_id == worker_id and entry.status in counts:
                counts[entry.status] += 1
        counts["total"] = sum(counts.values())
        return counts
    except Exception:
        return {}


def _time_ago(iso_ts: str) -> str:
    """Convert ISO timestamp to human-readable 'Xm ago' format."""
    # signed: beta
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta_s = (datetime.now(timezone.utc) - dt).total_seconds()
        if delta_s < 0:
            return "future"
        if delta_s < 60:
            return f"{int(delta_s)}s ago"
        if delta_s < 3600:
            return f"{int(delta_s / 60)}m ago"
        if delta_s < 86400:
            return f"{int(delta_s / 3600)}h ago"
        return f"{int(delta_s / 86400)}d ago"
    except Exception:
        return "unknown"


def get_worker_health(worker_id: str) -> Dict[str, Any]:
    """Full health report for a single external worker.

    Returns dict with:
        hwnd_status, uia_state, model_ok, last_heartbeat,
        last_result, quarantine_stats, site_health (if applicable)
    """
    # signed: beta
    registry = _load_registry()
    worker_info = registry.get("workers", {}).get(worker_id, {})
    health_data = _load_health_data()
    worker_health = health_data.get("workers", {}).get(worker_id, {})

    result: Dict[str, Any] = {
        "worker_id": worker_id,
        "registered": bool(worker_info),
        "registry_status": worker_info.get("status", "unknown"),
        "last_seen": worker_info.get("last_seen"),
        "task_count": worker_info.get("task_count", 0),
    }

    # HWND check (if worker has a known HWND)
    hwnd = worker_health.get("hwnd") or worker_info.get("hwnd")
    if hwnd:
        result["hwnd"] = hwnd
        result["hwnd_status"] = _check_hwnd_alive(int(hwnd))
        # UIA check (only if window is alive)
        if result["hwnd_status"]["alive"]:
            result["uia"] = _check_uia_state(int(hwnd))
        else:
            result["uia"] = {"state": "DEAD", "model_ok": False, "agent_ok": False}
    else:
        result["hwnd"] = None
        result["hwnd_status"] = {"alive": False, "visible": False, "note": "no HWND registered"}
        result["uia"] = {"state": "UNKNOWN", "model_ok": False, "agent_ok": False}

    # Heartbeat tracking
    last_hb = worker_health.get("last_heartbeat")
    result["last_heartbeat"] = last_hb
    result["heartbeat_age_s"] = None
    result["heartbeat_stale"] = True
    if last_hb:
        try:
            hb_dt = datetime.fromisoformat(last_hb)
            if hb_dt.tzinfo is None:
                hb_dt = hb_dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - hb_dt).total_seconds()
            result["heartbeat_age_s"] = round(age, 1)
            result["heartbeat_stale"] = age > HEARTBEAT_STALE_S
        except Exception:
            pass

    # Last task result timestamp
    result["last_result_at"] = worker_health.get("last_result_at")

    # Quarantine stats
    result["quarantine"] = _get_quarantine_stats_for_worker(worker_id)

    # Site health (for workers managing websites)
    site_url = worker_health.get("site_url") or worker_info.get("site_url")
    if site_url:
        result["site_health"] = _check_site_health(site_url)
        result["site_url"] = site_url
    else:
        result["site_health"] = None

    return result


def get_all_external_states() -> Dict[str, Dict[str, Any]]:
    """Get health state for ALL known external workers.

    Returns dict keyed by worker_id, each value is a health report
    from get_worker_health().
    """
    # signed: beta
    registry = _load_registry()
    health_data = _load_health_data()

    # Merge worker IDs from both sources
    all_ids: set = set()
    all_ids.update(registry.get("workers", {}).keys())
    all_ids.update(health_data.get("workers", {}).keys())

    states = {}
    for wid in sorted(all_ids):
        states[wid] = get_worker_health(wid)
    return states


def record_heartbeat(worker_id: str, hwnd: int | None = None,
                     site_url: str | None = None) -> None:
    """Record a heartbeat from an external worker.

    Called when a heartbeat message is detected on the bus, or
    can be called directly by external workers.
    """
    # signed: beta
    health_data = _load_health_data()
    workers = health_data.setdefault("workers", {})
    entry = workers.setdefault(worker_id, {})
    entry["last_heartbeat"] = _now_iso()
    if hwnd is not None:
        entry["hwnd"] = hwnd
    if site_url is not None:
        entry["site_url"] = site_url
    _save_health_data(health_data)


def record_result(worker_id: str) -> None:
    """Record that a result was received from an external worker."""
    # signed: beta
    health_data = _load_health_data()
    workers = health_data.setdefault("workers", {})
    entry = workers.setdefault(worker_id, {})
    entry["last_result_at"] = _now_iso()
    _save_health_data(health_data)


def check_heartbeat_alerts() -> List[Dict[str, Any]]:
    """Check for stale heartbeats and return alerts for missing workers.

    Returns list of alert dicts for workers whose heartbeat is stale
    (older than HEARTBEAT_STALE_S = 180s).
    """
    # signed: beta
    health_data = _load_health_data()
    alerts = []
    now = datetime.now(timezone.utc)

    for wid, wdata in health_data.get("workers", {}).items():
        last_hb = wdata.get("last_heartbeat")
        if not last_hb:
            continue
        try:
            hb_dt = datetime.fromisoformat(last_hb)
            if hb_dt.tzinfo is None:
                hb_dt = hb_dt.replace(tzinfo=timezone.utc)
            age = (now - hb_dt).total_seconds()
            if age > HEARTBEAT_STALE_S:
                alerts.append({
                    "worker_id": wid,
                    "last_heartbeat": last_hb,
                    "age_s": round(age, 1),
                    "threshold_s": HEARTBEAT_STALE_S,
                    "severity": "CRITICAL" if age > HEARTBEAT_STALE_S * 2 else "WARNING",
                })
        except Exception:
            pass

    return alerts


def cmd_health() -> None:
    """Full health report for all external workers (CLI command)."""
    # signed: beta
    states = get_all_external_states()
    if not states:
        print("No external workers registered.")
        print("Run 'scan' to discover workers from bus, or register manually.")
        return

    print(f"\n{'='*75}")
    print(f"  External Worker Health Report")
    print(f"  {_now_iso()}")
    print(f"{'='*75}\n")

    for wid, health in states.items():
        hwnd_ok = health.get("hwnd_status", {}).get("alive", False)
        uia_state = health.get("uia", {}).get("state", "UNKNOWN")
        model_ok = health.get("uia", {}).get("model_ok", False)
        hb_stale = health.get("heartbeat_stale", True)
        last_hb = health.get("last_heartbeat")

        # Status indicators
        hwnd_sym = "alive" if hwnd_ok else "dead/none"
        model_sym = "OK" if model_ok else "DRIFT" if hwnd_ok else "N/A"
        hb_sym = _time_ago(last_hb) if last_hb else "never"
        hb_warn = " STALE!" if hb_stale and last_hb else ""

        print(f"  {wid}")
        print(f"    Registry:   {health.get('registry_status', 'unknown')} "
              f"(tasks: {health.get('task_count', 0)})")
        print(f"    HWND:       {health.get('hwnd', 'none')} ({hwnd_sym})")
        print(f"    UIA State:  {uia_state}")
        print(f"    Model:      {health.get('uia', {}).get('model', 'unknown')} ({model_sym})")
        print(f"    Heartbeat:  {hb_sym}{hb_warn}")

        last_result = health.get("last_result_at")
        if last_result:
            print(f"    Last Result:{_time_ago(last_result)}")

        # Quarantine stats
        q = health.get("quarantine", {})
        if q:
            print(f"    Quarantine: P={q.get('PENDING',0)} V={q.get('VALIDATING',0)} "
                  f"A={q.get('APPROVED',0)} R={q.get('REJECTED',0)} "
                  f"E={q.get('EXPIRED',0)} (total={q.get('total',0)})")

        # Site health
        sh = health.get("site_health")
        if sh is not None:
            site_url = health.get("site_url", "?")
            if sh["reachable"]:
                print(f"    Site:       {site_url} -> {sh['status_code']} "
                      f"({sh['response_ms']}ms)")
            else:
                print(f"    Site:       {site_url} -> DOWN ({sh.get('error', 'unreachable')})")

        print()

    # Heartbeat alerts
    alerts = check_heartbeat_alerts()
    if alerts:
        print(f"  --- Heartbeat Alerts ---")
        for a in alerts:
            print(f"  [{a['severity']}] {a['worker_id']}: last heartbeat "
                  f"{_time_ago(a['last_heartbeat'])} (>{a['threshold_s']}s threshold)")
        print()


# ============================================================================ #
#                             INTERNAL HELPERS                                 #
# ============================================================================ #

def _find_dispatch_task(worker_id: str) -> Optional[str]:
    """Find the most recent un-received dispatch task for a worker."""  # signed: gamma
    log = _load_dispatch_log()
    for entry in reversed(log):
        if (entry.get("worker_id") == worker_id
                and not entry.get("result_received", False)):
            return entry.get("task")
    return None


def _mark_result_received(worker_id: str, quarantine_id: str) -> None:
    """Mark the most recent dispatch to a worker as result-received."""  # signed: gamma
    log = _load_dispatch_log()
    for entry in reversed(log):
        if (entry.get("worker_id") == worker_id
                and not entry.get("result_received", False)):
            entry["result_received"] = True
            entry["quarantine_id"] = quarantine_id
            entry["result_received_at"] = _now_iso()
            break
    _save_dispatch_log(log)

    # Update registry status
    registry = _load_registry()
    workers = registry.get("workers", {})
    if worker_id in workers:
        workers[worker_id]["status"] = "result_quarantined"
        workers[worker_id]["last_seen"] = _now_iso()
        registry["workers"] = workers
        _save_registry(registry)


def _request_cross_validation(quarantine_id: str, worker_id: str,
                               result_content: str) -> None:
    """Auto-request cross-validation when a result is quarantined."""  # signed: gamma
    _bus_publish({
        "sender": "external_monitor",
        "topic": "workers",
        "type": "cross_validation_request",
        "content": json.dumps({
            "quarantine_id": quarantine_id,
            "external_worker": worker_id,
            "result_preview": result_content[:200],
            "auto_requested": True,
            "requested_at": _now_iso(),
        }),
    })


# ============================================================================ #
#                                   CLI                                        #
# ============================================================================ #

def _cli() -> None:
    """Command-line interface for the external worker monitor."""  # signed: gamma
    parser = argparse.ArgumentParser(
        description="External Worker Monitor with Quarantine Integration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s status                        Show known external workers + alerts
  %(prog)s scan                          Discover workers from bus
  %(prog)s health                        Full health report for all workers
  %(prog)s dispatch blogs "Write post"   Send task to external worker
  %(prog)s list --limit 30               Recent external messages
  %(prog)s dispatch-q blogs "Review X"   Dispatch with quarantine tracking
  %(prog)s monitor --timeout 120         Watch bus, auto-quarantine results
  %(prog)s quarantine                    Show quarantine entries
  %(prog)s validate q_abc123def456       Trigger cross-validation
""",
    )
    sub = parser.add_subparsers(dest="command")

    # --- existing commands -------------------------------------------------- #
    sub.add_parser("status", help="Show all known external workers (enhanced)")
    sub.add_parser("scan", help="Discover external workers from bus")
    sub.add_parser("health", help="Full health report for all external workers")

    p_dispatch = sub.add_parser("dispatch", help="Send task to external worker")
    p_dispatch.add_argument("worker_id", help="External worker ID")
    p_dispatch.add_argument("task", help="Task description")

    p_list = sub.add_parser("list", help="List recent external worker messages")
    p_list.add_argument("--limit", type=int, default=20,
                        help="Number of messages to fetch (default: 20)")

    # --- quarantine-integrated commands ------------------------------------- #
    p_dq = sub.add_parser("dispatch-q",
                          help="Dispatch with quarantine tracking")
    p_dq.add_argument("worker_id", help="External worker ID")
    p_dq.add_argument("task", help="Task description")

    p_mon = sub.add_parser("monitor",
                           help="Watch bus for external results, auto-quarantine")
    p_mon.add_argument("--timeout", type=int, default=60,
                       help="Seconds to monitor (0=one-shot, default: 60)")
    p_mon.add_argument("--poll", type=float, default=3.0,
                       help="Poll interval in seconds (default: 3.0)")

    sub.add_parser("quarantine",
                   help="Show quarantine entries for external workers")

    p_val = sub.add_parser("validate",
                           help="Trigger cross-validation for quarantine entry")
    p_val.add_argument("quarantine_id", help="Quarantine entry ID (e.g. q_abc123)")

    # --- parse & dispatch --------------------------------------------------- #
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "status":
        cmd_status()
        # Enhanced: also show health summary
        print()
        alerts = check_heartbeat_alerts()
        if alerts:
            print(f"  Heartbeat Alerts ({len(alerts)}):")
            for a in alerts:
                print(f"    [{a['severity']}] {a['worker_id']}: "
                      f"last heartbeat {_time_ago(a['last_heartbeat'])}")
    elif args.command == "health":
        cmd_health()
    elif args.command == "scan":
        cmd_scan()
    elif args.command == "dispatch":
        cmd_dispatch(args.worker_id, args.task)
    elif args.command == "list":
        cmd_list(args.limit)
    elif args.command == "dispatch-q":
        dispatch_with_quarantine(args.worker_id, args.task)
    elif args.command == "monitor":
        monitor_results(timeout=args.timeout, poll_interval=args.poll)
    elif args.command == "quarantine":
        quarantine_status()
    elif args.command == "validate":
        validate_result(args.quarantine_id)
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
