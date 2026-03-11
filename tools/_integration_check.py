#!/usr/bin/env python3
"""One-shot integration check for all Skynet subsystems."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
os.chdir(str(ROOT))

DATA = ROOT / "data"
results = {}

# ═══ TEST 1: safe_kill() blocking ═══
print("=== TEST 1: safe_kill() blocking ===")
from skynet_process_guard import safe_kill, is_protected, refresh_registry

reg = refresh_registry()
procs = reg.get("processes", [])
print(f"  Protected processes: {len(procs)}")

blocked = 0
allowed = 0
for p in procs:
    pid = p.get("pid", 0)
    if not pid:
        continue
    result = safe_kill(pid, caller="delta_integration_test")
    if not result:
        blocked += 1
    else:
        allowed += 1

fake_result = safe_kill(99999, caller="delta_integration_test")
t1_pass = blocked > 0 and allowed == 0 and fake_result
print(f"  Blocked: {blocked}, Allowed: {allowed} (should be 0), Fake PID allowed: {fake_result}")
print(f"  VERDICT: {'PASS' if t1_pass else 'FAIL'}")
results["safe_kill_blocking"] = "PASS" if t1_pass else "FAIL"

# ═══ TEST 2: Watchdog integration wiring ═══
print("\n=== TEST 2: Watchdog integration wiring ===")
import inspect
import skynet_watchdog

checks = {
    "refresh_registry in restart_god_console": "_refresh_protected_registry" in inspect.getsource(skynet_watchdog.restart_god_console),
    "refresh_registry in restart_skynet": "_refresh_protected_registry" in inspect.getsource(skynet_watchdog.restart_skynet),
    "refresh_registry in restart_sse_daemon": "_refresh_protected_registry" in inspect.getsource(skynet_watchdog.restart_sse_daemon),
    "refresh_registry in daemon loop": "GUARD_REFRESH_INTERVAL" in inspect.getsource(skynet_watchdog.run_daemon),
    "_log_incident in restart_god_console": "_log_incident" in inspect.getsource(skynet_watchdog.restart_god_console),
    "_log_incident in restart_skynet": "_log_incident" in inspect.getsource(skynet_watchdog.restart_skynet),
    "_post_restart_alert in restart_god_console": "_post_restart_alert" in inspect.getsource(skynet_watchdog.restart_god_console),
    "_check_worker_hwnds in daemon loop": "_check_worker_hwnds" in inspect.getsource(skynet_watchdog.run_daemon),
    "_update_heartbeat in daemon loop": "_update_heartbeat" in inspect.getsource(skynet_watchdog.run_daemon),
}

all_pass = True
for name, result in checks.items():
    status = "PASS" if result else "FAIL"
    if not result:
        all_pass = False
    print(f"  {name}: {status}")
print(f"  VERDICT: {'PASS' if all_pass else 'FAIL'} ({sum(checks.values())}/{len(checks)})")
results["watchdog_wiring"] = f"{'PASS' if all_pass else 'FAIL'} ({sum(checks.values())}/{len(checks)})"

# ═══ TEST 3: data/incidents.json ═══
print("\n=== TEST 3: data/incidents.json ===")
inc_file = DATA / "incidents.json"
if inc_file.exists():
    incidents = json.loads(inc_file.read_text(encoding="utf-8"))
    print(f"  File exists: YES, {len(incidents)} incident(s)")
    for inc in incidents[-5:]:
        print(f"    - {inc.get('id', '?')} at {inc.get('timestamp', '?')}")
    results["incidents"] = f"{len(incidents)} recorded"
else:
    print("  File: NOT FOUND (no auto-restarts have occurred -- expected if services never died)")
    results["incidents"] = "none yet (expected)"

# ═══ TEST 4: data/critical_processes.json ═══
print("\n=== TEST 4: data/critical_processes.json ===")
cp_file = DATA / "critical_processes.json"
if cp_file.exists():
    reg = json.loads(cp_file.read_text(encoding="utf-8"))
    procs = reg.get("processes", [])
    real_pids = [p for p in procs if p.get("pid", 0) > 0]
    print(f"  Process count: {reg.get('process_count', 0)}")
    print(f"  Real PIDs: {len(real_pids)}/{len(procs)}")
    print(f"  Updated: {reg.get('updated_at', '?')}")
    for p in procs:
        pid = p.get("pid", 0)
        role = p.get("role", "?")
        name = p.get("name", "?")
        hwnd = p.get("hwnd", "")
        extra = f" hwnd={hwnd}" if hwnd else ""
        print(f"    PID {pid:>6d} | {role:<12s} | {name}{extra}")
    t4_pass = len(real_pids) >= 2  # at least skynet + god_console
    results["critical_processes"] = f"{'PASS' if t4_pass else 'FAIL'} ({len(real_pids)} real PIDs)"
else:
    print("  File: NOT FOUND")
    results["critical_processes"] = "FAIL (missing)"

# ═══ TEST 5: Running daemons ═══
print("\n=== TEST 5: Running daemons ===")

def port_pid(port):
    try:
        out = subprocess.check_output(["netstat", "-ano", "-p", "TCP"], text=True, timeout=5, stderr=subprocess.DEVNULL)
        for line in out.split("\n"):
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    try:
                        return int(parts[-1])
                    except ValueError:
                        pass
    except Exception:
        pass
    return 0

def find_python_pids(script_name):
    # Use Get-CimInstance (modern Windows) with wmic fallback
    for method in ["cim", "wmic"]:
        try:
            if method == "cim":
                ps_cmd = (
                    "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" "
                    "| ForEach-Object { $_.ProcessId.ToString() + '|' + $_.CommandLine }"
                )
                out = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    text=True, timeout=15, stderr=subprocess.DEVNULL
                )
                pids = []
                for line in out.strip().split("\n"):
                    line = line.strip()
                    if not line or "|" not in line:
                        continue
                    pid_str, cmdline = line.split("|", 1)
                    if script_name.lower() in cmdline.lower():
                        try:
                            pids.append(int(pid_str.strip()))
                        except ValueError:
                            pass
                if pids:
                    return pids
            else:
                out = subprocess.check_output(
                    ["wmic", "process", "where", "Name like '%python%'",
                     "get", "ProcessId,CommandLine", "/format:csv"],
                    text=True, timeout=10, stderr=subprocess.DEVNULL
                )
                pids = []
                for line in out.strip().split("\n"):
                    if script_name.lower() in line.lower():
                        parts = line.strip().split(",")
                        if parts:
                            try:
                                pids.append(int(parts[-1].strip()))
                            except ValueError:
                                pass
                return pids
        except Exception:
            continue
    return []

daemons = {}
daemons["skynet_backend"] = {"pid": port_pid(8420)}
daemons["god_console"] = {"pid": port_pid(8421)}
daemons["watchdog"] = {"pids": find_python_pids("skynet_watchdog")}
daemons["sse_daemon"] = {"pids": find_python_pids("skynet_sse_daemon")}
daemons["monitor"] = {"pids": find_python_pids("skynet_monitor")}
daemons["overseer"] = {"pids": find_python_pids("skynet_overseer")}

for name, info in daemons.items():
    pid_val = info.get("pid") or (info.get("pids", [None])[0] if info.get("pids") else None)
    pids = info.get("pids", [info.get("pid")] if info.get("pid") else [])
    running = bool(pid_val)
    print(f"  {name:<18s}: {'RUNNING' if running else 'DOWN':<8s} PIDs={pids}")
    daemons[name]["running"] = running

running_count = sum(1 for d in daemons.values() if d.get("running"))
total = len(daemons)
results["daemons"] = f"{running_count}/{total} running"

# Heartbeat file
hb_path = DATA / "service_heartbeats.json"
if hb_path.exists():
    hb = json.loads(hb_path.read_text(encoding="utf-8"))
    print(f"\n  Heartbeat file: {len(hb)} services tracked")
    for svc, info in hb.items():
        age = time.time() - info.get("last_seen", 0)
        print(f"    {svc}: {info.get('status')} (age={age:.0f}s)")
else:
    print("\n  Heartbeat file: not yet created (watchdog v2 not restarted)")

# Watchdog status
ws_path = DATA / "watchdog_status.json"
if ws_path.exists():
    ws = json.loads(ws_path.read_text(encoding="utf-8"))
    print(f"  Watchdog status: god={ws.get('god_console')}, skynet={ws.get('skynet')}, updated={ws.get('updated', '?')}")

# ═══ SUMMARY ═══
print("\n" + "=" * 55)
print("  INTEGRATION CHECK SUMMARY")
print("=" * 55)
for k, v in results.items():
    print(f"  {k:<25s}: {v}")

# Post to bus  # signed: alpha
from tools.skynet_spam_guard import guarded_publish
summary = "; ".join(f"{k}={v}" for k, v in results.items())
try:
    guarded_publish({
        "sender": "delta",
        "topic": "orchestrator",
        "type": "result",
        "content": f"INTEGRATION CHECK: {summary}"
    })
    print("\n  Results posted to bus.")
except Exception as e:
    print(f"\n  Bus post failed: {e}")
