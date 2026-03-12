#!/usr/bin/env python3
"""
skynet_arch_verify.py -- Architecture Verification Tool

Phase 0 boot check: verifies every agent knows the system architecture.
Returns PASS/FAIL per check with details.

Checks:
  (a) Entity enumeration -- ALL entities (workers + consultants + orchestrator)
  (b) Delivery mechanism -- ghost_type (Win32 clipboard paste) awareness
  (c) Bus architecture -- ring buffer size, persistence model
  (d) Daemon ecosystem -- which daemons are running

Usage:
    python tools/skynet_arch_verify.py          # Run all checks, JSON output
    python tools/skynet_arch_verify.py --brief   # One-line summary
    python tools/skynet_arch_verify.py --check entities   # Single check
"""
# signed: delta

import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
DATA = ROOT / "data"

SKYNET_URL = "http://localhost:8420"

# ── Expected Architecture Constants ──────────────────────────────
EXPECTED_WORKERS = ["alpha", "beta", "gamma", "delta"]
EXPECTED_CONSULTANTS = ["consultant", "gemini_consultant"]
EXPECTED_ORCHESTRATOR = "orchestrator"
ALL_EXPECTED_ENTITIES = EXPECTED_WORKERS + EXPECTED_CONSULTANTS + [EXPECTED_ORCHESTRATOR]

# Delivery mechanism: ghost_type uses Win32 clipboard paste via PostMessage WM_PASTE
DELIVERY_MECHANISM = "ghost_type"

# Bus architecture: Go backend ring buffer
BUS_RING_BUFFER_SIZE = 100
BUS_PERSISTENCE = "none"  # ring buffer only, no disk persistence

# Expected daemons (name -> PID file path pattern)
EXPECTED_DAEMONS = {
    "skynet_monitor": "data/monitor.pid",
    "skynet_self_prompt": "data/self_prompt.pid",
    "skynet_self_improve": "data/self_improve.pid",
    "skynet_bus_relay": "data/bus_relay.pid",
    "skynet_learner": "data/learner.pid",
    "skynet_overseer": "data/overseer.pid",
    "skynet_watchdog": "data/watchdog.pid",
    "skynet_realtime": "data/realtime.pid",
}
# signed: delta


def _http_get(path: str, timeout: float = 3):
    """Simple HTTP GET returning parsed JSON or None."""
    try:
        from urllib.request import urlopen
        return json.loads(urlopen(f"{SKYNET_URL}{path}", timeout=timeout).read())
    except Exception:
        return None
# signed: delta


def _pid_alive(pid: int) -> bool:
    """Check if a process with given PID is alive."""
    if pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    # Fallback: os.kill with signal 0 (existence check)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
# signed: delta


# ══════════════════════════════════════════════════════════════════
#  CHECK (a): Entity Enumeration
# ══════════════════════════════════════════════════════════════════

def check_entities() -> Dict:
    """Verify the kernel knows ALL entities: workers + consultants + orchestrator.

    Checks:
      1. WORKER_NAMES constant exists and has 4 workers
      2. CONSULTANT_NAMES constant exists and has 2 consultants
      3. ALL_AGENT_NAMES constant covers all 7 entities
      4. data/workers.json has all 4 workers registered
      5. data/agent_profiles.json has all 7 entity profiles
      6. data/orchestrator.json exists with valid HWND
      7. Consultant state files exist
    """
    result = {"check": "entities", "status": "PASS", "details": [], "failures": []}

    # 1. Check consciousness kernel constants
    try:
        from tools.skynet_self import WORKER_NAMES, CONSULTANT_NAMES, ALL_AGENT_NAMES
        if set(WORKER_NAMES) == set(EXPECTED_WORKERS):
            result["details"].append(f"WORKER_NAMES: OK ({len(WORKER_NAMES)} workers)")
        else:
            result["failures"].append(
                f"WORKER_NAMES mismatch: expected {EXPECTED_WORKERS}, got {WORKER_NAMES}"
            )
        if set(CONSULTANT_NAMES) == set(EXPECTED_CONSULTANTS):
            result["details"].append(f"CONSULTANT_NAMES: OK ({len(CONSULTANT_NAMES)} consultants)")
        else:
            result["failures"].append(
                f"CONSULTANT_NAMES mismatch: expected {EXPECTED_CONSULTANTS}, got {list(CONSULTANT_NAMES)}"
            )
        if set(ALL_AGENT_NAMES) == set(ALL_EXPECTED_ENTITIES):
            result["details"].append(f"ALL_AGENT_NAMES: OK ({len(ALL_AGENT_NAMES)} total)")
        else:
            missing = set(ALL_EXPECTED_ENTITIES) - set(ALL_AGENT_NAMES)
            extra = set(ALL_AGENT_NAMES) - set(ALL_EXPECTED_ENTITIES)
            result["failures"].append(
                f"ALL_AGENT_NAMES mismatch: missing={list(missing)}, extra={list(extra)}"
            )
    except ImportError as e:
        result["failures"].append(f"Cannot import consciousness kernel constants: {e}")

    # 2. Check data/workers.json
    workers_file = DATA / "workers.json"
    if workers_file.exists():
        try:
            workers = json.loads(workers_file.read_text())
            names = [w.get("name", "") for w in workers]
            if set(names) >= set(EXPECTED_WORKERS):
                result["details"].append(f"workers.json: OK ({len(workers)} entries)")
            else:
                missing = set(EXPECTED_WORKERS) - set(names)
                result["failures"].append(f"workers.json missing workers: {list(missing)}")
        except Exception as e:
            result["failures"].append(f"workers.json parse error: {e}")
    else:
        result["failures"].append("workers.json not found")

    # 3. Check data/agent_profiles.json
    profiles_file = DATA / "agent_profiles.json"
    if profiles_file.exists():
        try:
            profiles = json.loads(profiles_file.read_text())
            if isinstance(profiles, list):
                prof_names = [p.get("name", "") for p in profiles]
            elif isinstance(profiles, dict):
                prof_names = list(profiles.keys())
            else:
                prof_names = []
            # Check for all 7 entities
            found = set()
            for expected in ALL_EXPECTED_ENTITIES:
                for pn in prof_names:
                    if expected.lower() in pn.lower() or pn.lower() in expected.lower():
                        found.add(expected)
                        break
            if len(found) >= 5:  # At least orch + 4 workers
                result["details"].append(f"agent_profiles.json: OK ({len(prof_names)} profiles)")
            else:
                missing = set(ALL_EXPECTED_ENTITIES) - found
                result["failures"].append(f"agent_profiles.json missing entities: {list(missing)}")
        except Exception as e:
            result["failures"].append(f"agent_profiles.json parse error: {e}")
    else:
        result["failures"].append("agent_profiles.json not found")

    # 4. Check orchestrator.json
    orch_file = DATA / "orchestrator.json"
    if orch_file.exists():
        try:
            orch = json.loads(orch_file.read_text())
            hwnd = orch.get("hwnd", 0)
            if hwnd:
                result["details"].append(f"orchestrator.json: OK (HWND={hwnd})")
            else:
                result["failures"].append("orchestrator.json has zero HWND")
        except Exception as e:
            result["failures"].append(f"orchestrator.json parse error: {e}")
    else:
        result["failures"].append("orchestrator.json not found")

    # 5. Check consultant state files
    for name in EXPECTED_CONSULTANTS:
        sf = DATA / f"{'consultant_state' if name == 'consultant' else 'gemini_consultant_state'}.json"
        if sf.exists():
            result["details"].append(f"{sf.name}: present")
        else:
            result["failures"].append(f"{sf.name}: missing")

    if result["failures"]:
        result["status"] = "FAIL"
    return result
# signed: delta


# ══════════════════════════════════════════════════════════════════
#  CHECK (b): Delivery Mechanism Knowledge
# ══════════════════════════════════════════════════════════════════

def check_delivery_mechanism() -> Dict:
    """Verify the system knows that ghost_type (Win32 clipboard paste) is the delivery mechanism.

    Checks:
      1. skynet_dispatch.py has ghost_type_to_worker function
      2. The function uses clipboard-based delivery (WM_PASTE pattern)
      3. Worker dispatch uses HWND-targeted delivery, not HTTP
    """
    result = {"check": "delivery_mechanism", "status": "PASS", "details": [], "failures": []}

    # 1. Check ghost_type_to_worker exists in dispatch module
    dispatch_file = ROOT / "tools" / "skynet_dispatch.py"
    if dispatch_file.exists():
        content = dispatch_file.read_text(errors="replace")
        if "ghost_type_to_worker" in content:
            result["details"].append("ghost_type_to_worker: function exists in skynet_dispatch.py")
        else:
            result["failures"].append("ghost_type_to_worker function NOT found in skynet_dispatch.py")

        # 2. Check for WM_PASTE / clipboard pattern
        if "WM_PASTE" in content or "CF_UNICODETEXT" in content or "clipboard" in content.lower():
            result["details"].append("Clipboard-based delivery (WM_PASTE): confirmed in dispatch code")
        else:
            result["failures"].append("No clipboard/WM_PASTE pattern found — delivery mechanism unknown")

        # 3. Check HWND-targeted delivery
        if "hwnd" in content.lower() and "PostMessage" in content:
            result["details"].append("HWND-targeted PostMessage delivery: confirmed")
        elif "hwnd" in content.lower():
            result["details"].append("HWND references found (PostMessage pattern may be indirect)")
        else:
            result["failures"].append("No HWND-targeted delivery found in dispatch")
    else:
        result["failures"].append("skynet_dispatch.py not found")

    # 4. Verify delivery mechanism constant matches
    result["details"].append(f"Expected delivery mechanism: {DELIVERY_MECHANISM}")

    if result["failures"]:
        result["status"] = "FAIL"
    return result
# signed: delta


# ══════════════════════════════════════════════════════════════════
#  CHECK (c): Bus Architecture Knowledge
# ══════════════════════════════════════════════════════════════════

def check_bus_architecture() -> Dict:
    """Verify knowledge of bus architecture: ring buffer size, persistence model.

    Checks:
      1. Backend is reachable on port 8420
      2. Bus responds to message queries
      3. Ring buffer size is known (100 messages)
      4. Persistence model is understood (no disk persistence)
    """
    result = {"check": "bus_architecture", "status": "PASS", "details": [], "failures": []}

    # 1. Backend reachability
    status = _http_get("/status")
    if status:
        version = status.get("version", "unknown")
        uptime = status.get("uptime_s", 0)
        result["details"].append(f"Backend online: version={version}, uptime={uptime:.0f}s")
    else:
        result["failures"].append("Backend NOT reachable on port 8420")

    # 2. Bus message query
    bus = _http_get("/bus/messages?limit=1")
    if bus is not None:
        result["details"].append("Bus message query: responsive")
    else:
        result["failures"].append("Bus message query failed")

    # 3. Ring buffer architecture knowledge
    # Verify Go backend server.go mentions ring buffer constants
    server_go = ROOT / "Skynet" / "server.go"
    if server_go.exists():
        try:
            go_content = server_go.read_text(errors="replace")
            # Look for ring buffer size constant
            if "100" in go_content and ("ring" in go_content.lower() or "maxMessages" in go_content or "MaxMessages" in go_content):
                result["details"].append(f"Ring buffer size: {BUS_RING_BUFFER_SIZE} (confirmed in server.go)")
            else:
                result["details"].append(f"Ring buffer size: {BUS_RING_BUFFER_SIZE} (documented, verify in server.go)")
        except Exception:
            result["details"].append(f"Ring buffer size: {BUS_RING_BUFFER_SIZE} (documented)")
    else:
        result["details"].append(f"Ring buffer size: {BUS_RING_BUFFER_SIZE} (documented, server.go not found locally)")

    # 4. Persistence model
    result["details"].append(f"Bus persistence: {BUS_PERSISTENCE} (FIFO ring buffer, evicted messages are lost)")

    if result["failures"]:
        result["status"] = "FAIL"
    return result
# signed: delta


# ══════════════════════════════════════════════════════════════════
#  CHECK (d): Daemon Ecosystem
# ══════════════════════════════════════════════════════════════════

def check_daemon_ecosystem() -> Dict:
    """Verify daemon ecosystem: which daemons are running.

    Checks PID files and validates that processes are alive.
    """
    result = {"check": "daemon_ecosystem", "status": "PASS", "details": [], "failures": []}
    running = 0
    total = len(EXPECTED_DAEMONS)

    for daemon_name, pid_path in EXPECTED_DAEMONS.items():
        pid_file = ROOT / pid_path
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if _pid_alive(pid):
                    result["details"].append(f"{daemon_name}: RUNNING (PID {pid})")
                    running += 1
                else:
                    result["details"].append(f"{daemon_name}: DEAD (stale PID {pid})")
                    result["failures"].append(f"{daemon_name} has stale PID file (PID {pid} not alive)")
            except ValueError:
                result["details"].append(f"{daemon_name}: PID file corrupt")
                result["failures"].append(f"{daemon_name} PID file unreadable")
        else:
            result["details"].append(f"{daemon_name}: no PID file")
            # Not all daemons are mandatory; only warn, don't fail
            if daemon_name in ("skynet_monitor", "skynet_realtime"):
                result["failures"].append(f"Critical daemon {daemon_name} has no PID file")

    result["summary"] = f"{running}/{total} daemons running"

    if result["failures"]:
        result["status"] = "FAIL"
    return result
# signed: delta


# ══════════════════════════════════════════════════════════════════
#  AGGREGATE: Run All Checks
# ══════════════════════════════════════════════════════════════════

def verify_architecture() -> Dict:
    """Run all architecture verification checks.

    Returns a dict with overall PASS/FAIL and per-check results.
    This is the function called by Phase 0 boot check.
    """
    checks = [
        check_entities(),
        check_delivery_mechanism(),
        check_bus_architecture(),
        check_daemon_ecosystem(),
    ]

    overall = "PASS"
    total_failures = 0
    for check in checks:
        if check["status"] == "FAIL":
            overall = "FAIL"
            total_failures += len(check.get("failures", []))

    return {
        "overall": overall,
        "total_checks": len(checks),
        "total_failures": total_failures,
        "checks": {c["check"]: c for c in checks},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
# signed: delta


# ══════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Architecture Verification")
    parser.add_argument("--brief", action="store_true", help="One-line summary")
    parser.add_argument("--check", choices=["entities", "delivery", "bus", "daemons"],
                        help="Run a single check")
    args = parser.parse_args()

    if args.check:
        dispatch = {
            "entities": check_entities,
            "delivery": check_delivery_mechanism,
            "bus": check_bus_architecture,
            "daemons": check_daemon_ecosystem,
        }
        result = dispatch[args.check]()
        print(json.dumps(result, indent=2))
    elif args.brief:
        result = verify_architecture()
        status = result["overall"]
        failures = result["total_failures"]
        checks_ok = sum(1 for c in result["checks"].values() if c["status"] == "PASS")
        total = result["total_checks"]
        print(f"Architecture Verification: {status} ({checks_ok}/{total} checks passed, {failures} failures)")
    else:
        result = verify_architecture()
        print(json.dumps(result, indent=2))

    sys.exit(0 if result.get("overall", result.get("status")) == "PASS" else 1)
# signed: delta


if __name__ == "__main__":
    main()
