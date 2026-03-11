#!/usr/bin/env python3
"""Skynet Kill Authorization Protocol — consensus-based process termination.

Workers NEVER kill processes directly. They request authorization via bus.
Orchestrator broadcasts consensus check to all workers before authorizing.

Flow:
  1. Worker calls request_kill(pid, name, reason, requester)
  2. Orchestrator receives kill_request on bus
  3. If PID in critical_processes.json → DENY immediately
  4. Orchestrator broadcasts kill_consensus_check to all workers
  5. Workers vote safe=true/false within 30s deadline
  6. ALL 4 must vote safe=true → orchestrator may authorize
  7. Any safe=false or missing vote → auto-DENY
  8. Full vote record logged to data/kill_log.json
"""

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
KILL_LOG = DATA / "kill_log.json"
PENDING_FILE = DATA / "kill_pending.json"
CRITICAL_FILE = DATA / "critical_processes.json"
BUS_URL = "http://localhost:8420/bus/publish"
BUS_POLL = "http://localhost:8420/bus/messages"

WORKERS = ["alpha", "beta", "gamma", "delta"]
CONSENSUS_TIMEOUT = 30  # seconds


# ── Helpers ───────────────────────────────────────────────────────────────

def _bus_post(msg):
    """Post a message to the Skynet bus."""
    from tools.shared.bus import bus_post
    return bus_post(msg)


def _bus_poll(limit=50):
    """Poll bus for messages."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{BUS_POLL}?limit={limit}", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return []


def _load_json(path):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _is_protected(pid=None, name=None):
    """Check critical_processes.json. Returns (protected, reason)."""
    try:
        if not CRITICAL_FILE.exists():
            return False, ""
        data = json.loads(CRITICAL_FILE.read_text(encoding="utf-8"))
        if name:
            for pn in data.get("protected_names", []):
                if pn.lower() in name.lower() or name.lower() in pn.lower():
                    return True, f"Protected service: {pn}"
        if pid:
            for proc in data.get("processes", []):
                if proc.get("pid") == pid or proc.get("hwnd") == pid:
                    return True, f"Protected {proc.get('role', '?')}: {proc.get('name', '?')}"
    except Exception:
        pass
    return False, ""


# ── Kill Log ──────────────────────────────────────────────────────────────

def _append_kill_log(entry):
    logs = _load_json(KILL_LOG)
    if not isinstance(logs, list):
        logs = []
    logs.append(entry)
    if len(logs) > 500:
        logs = logs[-500:]
    _save_json(KILL_LOG, logs)


# ── Pending Requests ──────────────────────────────────────────────────────

def _load_pending():
    data = _load_json(PENDING_FILE)
    return data if isinstance(data, list) else []


def _save_pending(pending):
    _save_json(PENDING_FILE, pending)


def _add_pending(request):
    pending = _load_pending()
    pending.append(request)
    _save_pending(pending)


def _remove_pending(request_id):
    pending = _load_pending()
    pending = [p for p in pending if p.get("request_id") != request_id]
    _save_pending(pending)


def _get_pending(request_id):
    for p in _load_pending():
        if p.get("request_id") == request_id:
            return p
    return None


# ── Worker Side: Request Kill ─────────────────────────────────────────────

def request_kill(pid, name, reason, requester):
    """Worker requests process termination. Posts to bus for orchestrator.

    Returns request_id for tracking.
    """
    request_id = f"kill_{int(time.time())}_{requester}_{uuid.uuid4().hex[:6]}"

    msg = {
        "sender": requester,
        "topic": "orchestrator",
        "type": "kill_request",
        "content": json.dumps({
            "request_id": request_id,
            "pid": pid,
            "name": name,
            "reason": reason,
            "requester": requester,
            "timestamp": datetime.now().isoformat(),
        }),
    }

    ok = _bus_post(msg)
    if ok:
        print(f"[kill-auth] Kill request posted: {request_id} (pid={pid} name={name})")
    else:
        print(f"[kill-auth] ERROR: Failed to post kill request to bus")
    return request_id


# ── Worker Side: Vote on Consensus Check ─────────────────────────────────

def vote_kill(request_id, worker, safe, reason=""):
    """Worker votes on a kill consensus check.

    Args:
        request_id: The kill request being voted on
        worker: This worker's name
        safe: True if safe to kill, False if this worker depends on the process
        reason: Explanation for vote
    """
    msg = {
        "sender": worker,
        "topic": "orchestrator",
        "type": "kill_consensus_vote",
        "content": json.dumps({
            "request_id": request_id,
            "worker": worker,
            "safe": safe,
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
        }),
    }
    return _bus_post(msg)


# ── Orchestrator Side: Broadcast Consensus Check ─────────────────────────

def broadcast_consensus_check(request_id, pid, name, reason, requester):
    """Orchestrator broadcasts kill_consensus_check to all workers."""
    deadline = (datetime.now() + timedelta(seconds=CONSENSUS_TIMEOUT)).isoformat()

    msg = {
        "sender": "orchestrator",
        "topic": "workers",
        "type": "kill_consensus_check",
        "content": json.dumps({
            "request_id": request_id,
            "pid": pid,
            "name": name,
            "reason": reason,
            "requester": requester,
            "deadline": deadline,
        }),
    }
    ok = _bus_post(msg)
    if ok:
        print(f"[kill-auth] Consensus check broadcast for {request_id} (deadline: {CONSENSUS_TIMEOUT}s)")
    return ok


# ── Orchestrator Side: Collect Votes ──────────────────────────────────────

def collect_votes(request_id, timeout=CONSENSUS_TIMEOUT):
    """Wait for all 4 workers to vote. Returns (all_safe, votes_dict)."""
    deadline = time.time() + timeout
    votes = {}

    while time.time() < deadline and len(votes) < len(WORKERS):
        msgs = _bus_poll(100)
        for m in msgs:
            if m.get("type") != "kill_consensus_vote":
                continue
            try:
                content = m.get("content", "")
                if isinstance(content, str):
                    content = json.loads(content)
                if content.get("request_id") != request_id:
                    continue
                worker = content.get("worker")
                if worker and worker not in votes:
                    votes[worker] = {
                        "safe": content.get("safe", False),
                        "reason": content.get("reason", ""),
                        "timestamp": content.get("timestamp", ""),
                    }
                    print(f"[kill-auth] Vote from {worker}: safe={content.get('safe')} - {content.get('reason', '')}")
            except Exception:
                continue
        if len(votes) < len(WORKERS):
            time.sleep(1)

    # Mark missing workers as abstain (= block)
    for w in WORKERS:
        if w not in votes:
            votes[w] = {"safe": False, "reason": "NO RESPONSE (timeout=block)", "timestamp": ""}

    all_safe = all(v["safe"] for v in votes.values())
    return all_safe, votes


# ── Orchestrator Side: Full Authorization Flow ───────────────────────────

def process_kill_request(request_data):
    """Full orchestrator-side kill authorization flow."""
    request_id = request_data["request_id"]
    pid = request_data.get("pid")
    name = request_data.get("name", "unknown")
    reason = request_data.get("reason", "no reason given")
    requester = request_data.get("requester", "unknown")

    log_entry = {
        "request_id": request_id, "pid": pid, "name": name,
        "reason": reason, "requester": requester,
        "timestamp": datetime.now().isoformat(),
    }

    protected, protect_reason = _is_protected(pid=pid, name=name)
    if protected:
        return _deny_protected(log_entry, request_id, protect_reason)

    _add_pending({
        "request_id": request_id, "pid": pid, "name": name,
        "reason": reason, "requester": requester,
        "status": "voting", "timestamp": datetime.now().isoformat(),
    })
    broadcast_consensus_check(request_id, pid, name, reason, requester)

    all_safe, votes = collect_votes(request_id)
    log_entry["votes"] = votes

    if all_safe:
        return _authorize_consensus(log_entry, request_id, votes)
    return _deny_consensus(log_entry, request_id, votes)


def _deny_protected(log_entry, request_id, protect_reason):
    """Deny a kill request for a protected process."""
    log_entry["decision"] = "DENIED"
    log_entry["deny_reason"] = f"PROTECTED: {protect_reason}"
    log_entry["votes"] = {}
    _append_kill_log(log_entry)
    _bus_post({"sender": "orchestrator", "topic": "workers", "type": "kill_denied",
               "content": json.dumps({"request_id": request_id, "reason": protect_reason})})
    print(f"[kill-auth] DENIED (protected): {request_id} - {protect_reason}")
    return False, protect_reason, {}


def _authorize_consensus(log_entry, request_id, votes):
    """Authorize a kill after all workers voted safe."""
    log_entry["decision"] = "AUTHORIZED"
    _append_kill_log(log_entry)
    _remove_pending(request_id)
    print(f"[kill-auth] AUTHORIZED: {request_id} (all workers voted safe)")
    return True, "All workers voted safe", votes


def _deny_consensus(log_entry, request_id, votes):
    """Deny a kill when one or more workers voted unsafe."""
    blockers = [f"{w}: {v['reason']}" for w, v in votes.items() if not v["safe"]]
    deny_reason = "Blocked by: " + "; ".join(blockers)
    log_entry["decision"] = "DENIED"
    log_entry["deny_reason"] = deny_reason
    _append_kill_log(log_entry)
    _remove_pending(request_id)
    _bus_post({"sender": "orchestrator", "topic": "workers", "type": "kill_denied",
               "content": json.dumps({"request_id": request_id, "reason": deny_reason})})
    print(f"[kill-auth] DENIED: {request_id} - {deny_reason}")
    return False, deny_reason, votes


def execute_kill(pid):
    """Actually terminate a process after authorization. Orchestrator only."""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"],
            capture_output=True, text=True, timeout=10,
        )
        return True
    except Exception as e:
        return False


# ── Manual orchestrator authorize/deny (for /kill/* endpoints) ────────────

def authorize_kill_manual(request_id):
    """Orchestrator manually authorizes a pending kill (skips consensus for override)."""
    req = _get_pending(request_id)
    if not req:
        return False, "Request not found"

    pid = req.get("pid")
    protected, reason = _is_protected(pid=pid, name=req.get("name"))
    if protected:
        return False, f"PROTECTED: {reason}"

    ok = execute_kill(pid)
    log_entry = {
        "request_id": request_id,
        "pid": pid,
        "name": req.get("name"),
        "decision": "AUTHORIZED_MANUAL",
        "executed": ok,
        "authorized_by": "orchestrator",
        "timestamp": datetime.now().isoformat(),
    }
    _append_kill_log(log_entry)
    _remove_pending(request_id)
    _bus_post({
        "sender": "orchestrator",
        "topic": "workers",
        "type": "kill_authorized",
        "content": json.dumps({"request_id": request_id, "pid": pid, "executed": ok}),
    })
    return ok, "Authorized and executed" if ok else "Authorized but execution failed"


def deny_kill_manual(request_id, reason="Orchestrator denied"):
    """Orchestrator manually denies a pending kill."""
    req = _get_pending(request_id)
    if not req:
        return False, "Request not found"

    log_entry = {
        "request_id": request_id,
        "pid": req.get("pid"),
        "name": req.get("name"),
        "decision": "DENIED_MANUAL",
        "deny_reason": reason,
        "timestamp": datetime.now().isoformat(),
    }
    _append_kill_log(log_entry)
    _remove_pending(request_id)
    _bus_post({
        "sender": "orchestrator",
        "topic": "workers",
        "type": "kill_denied",
        "content": json.dumps({"request_id": request_id, "reason": reason}),
    })
    return True, "Denied"


# ── Query ─────────────────────────────────────────────────────────────────

def get_pending_requests():
    return _load_pending()


def get_kill_log(limit=20):
    logs = _load_json(KILL_LOG)
    if isinstance(logs, list):
        return logs[-limit:]
    return []


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Kill Authorization Protocol")
    sub = parser.add_subparsers(dest="cmd")

    req = sub.add_parser("request", help="Worker requests kill")
    req.add_argument("--pid", type=int, required=True)
    req.add_argument("--name", default="unknown")
    req.add_argument("--reason", default="manual request")
    req.add_argument("--requester", default="unknown")

    sub.add_parser("pending", help="Show pending requests")
    sub.add_parser("log", help="Show kill log")

    auth = sub.add_parser("authorize", help="Orchestrator authorizes kill")
    auth.add_argument("request_id")

    deny = sub.add_parser("deny", help="Orchestrator denies kill")
    deny.add_argument("request_id")
    deny.add_argument("--reason", default="Orchestrator denied")

    vote_p = sub.add_parser("vote", help="Worker votes on consensus check")
    vote_p.add_argument("request_id")
    vote_p.add_argument("--worker", required=True)
    vote_p.add_argument("--safe", type=bool, default=True)
    vote_p.add_argument("--reason", default="")

    sub.add_parser("test", help="Run simulation test")

    args = parser.parse_args()
    _dispatch_cli_command(args, parser)


def _dispatch_cli_command(args, parser):
    """Route CLI subcommand to the appropriate handler."""
    if args.cmd == "request":
        rid = request_kill(args.pid, args.name, args.reason, args.requester)
        print(f"Request ID: {rid}")
    elif args.cmd == "pending":
        pending = get_pending_requests()
        if not pending:
            print("No pending kill requests.")
        for p in pending:
            print(json.dumps(p, indent=2))
    elif args.cmd == "log":
        for entry in get_kill_log():
            print(json.dumps(entry, indent=2))
    elif args.cmd == "authorize":
        ok, msg = authorize_kill_manual(args.request_id)
        print(f"{'OK' if ok else 'FAIL'}: {msg}")
    elif args.cmd == "deny":
        ok, msg = deny_kill_manual(args.request_id, args.reason)
        print(f"{'OK' if ok else 'FAIL'}: {msg}")
    elif args.cmd == "vote":
        ok = vote_kill(args.request_id, args.worker, args.safe, args.reason)
        print(f"Vote posted: {ok}")
    elif args.cmd == "test":
        run_test()
    else:
        parser.print_help()


def run_test():
    """Simulate the full kill auth flow without actually killing anything."""
    print("=" * 60)
    print("KILL AUTHORIZATION PROTOCOL SIMULATION")
    print("=" * 60)
    _test_protected_process_deny()
    _test_worker_request_posts_to_bus()
    _test_vote_mechanism()
    _test_pending_request_tracking()
    _test_manual_deny()
    _test_kill_log_persistence()
    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


def _test_protected_process_deny():
    print("\n[TEST 1] Request kill of protected process (skynet.exe)")
    result = process_kill_request({
        "request_id": "test_protected", "pid": 24072,
        "name": "skynet.exe", "reason": "test", "requester": "gamma",
    })
    assert result[0] == False, "Should deny protected process"
    print("  PASS: Protected process denied immediately")


def _test_worker_request_posts_to_bus():
    print("\n[TEST 2] Worker posts kill request to bus")
    rid = request_kill(99999, "test_proc.exe", "consuming 100% CPU", "alpha")
    assert rid.startswith("kill_"), "Should return request_id"
    print(f"  PASS: Request posted with id={rid}")


def _test_vote_mechanism():
    print("\n[TEST 3] Workers vote on consensus check")
    test_rid = "test_vote_" + uuid.uuid4().hex[:6]
    vote_kill(test_rid, "alpha", True, "Not my dependency")
    vote_kill(test_rid, "beta", True, "Not my dependency")
    vote_kill(test_rid, "gamma", False, "I am using this process for OCR")
    vote_kill(test_rid, "delta", True, "Not my dependency")
    print("  PASS: All 4 votes posted (gamma voted safe=false)")


def _test_pending_request_tracking():
    print("\n[TEST 4] Pending request tracking")
    _add_pending({"request_id": "test_pending_1", "pid": 12345, "name": "dummy", "status": "voting"})
    pending = get_pending_requests()
    assert any(p["request_id"] == "test_pending_1" for p in pending), "Should find pending request"
    _remove_pending("test_pending_1")
    pending2 = get_pending_requests()
    assert not any(p["request_id"] == "test_pending_1" for p in pending2), "Should be removed"
    print("  PASS: Pending add/remove works")


def _test_manual_deny():
    print("\n[TEST 5] Manual deny by orchestrator")
    _add_pending({"request_id": "test_deny_1", "pid": 55555, "name": "some_proc"})
    ok, msg = deny_kill_manual("test_deny_1", "Not authorized by GOD")
    assert ok, "Should succeed"
    print(f"  PASS: Denied with reason: {msg}")


def _test_kill_log_persistence():
    print("\n[TEST 6] Kill log persistence")
    logs = get_kill_log(50)
    assert len(logs) > 0, "Should have log entries from tests"
    print(f"  PASS: {len(logs)} entries in kill log")


if __name__ == "__main__":
    main()
