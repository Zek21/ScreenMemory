#!/usr/bin/env python3
"""
convene_gate.py -- Convene-First Communication Middleware.

Monitors the bus for worker reports destined for orchestrator,
intercepts them, routes through convene consensus first.
Only after majority approval does the message reach orchestrator.

Usage:
    python convene_gate.py --monitor           # run the gate monitor daemon
    python convene_gate.py --propose "report"  # propose a report (as worker)
    python convene_gate.py --vote GATE_ID      # vote YES on a pending proposal
    python convene_gate.py --reject GATE_ID    # vote NO on a pending proposal
    python convene_gate.py --pending           # show pending proposals
    python convene_gate.py --stats             # show gate statistics
    python convene_gate.py --test              # run protocol simulation
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import requests

SKYNET = "http://localhost:8420"
BUS_PUBLISH = f"{SKYNET}/bus/publish"
BUS_MESSAGES = f"{SKYNET}/bus/messages"
WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]


def _bus_post(sender, topic, msg_type, content):
    """Post to bus with retry."""
    for attempt in range(3):
        try:
            r = requests.post(BUS_PUBLISH, json={
                "sender": sender, "topic": topic,
                "type": msg_type, "content": content,
            }, timeout=3)
            return r.ok
        except Exception:
            if attempt < 2:
                time.sleep(0.5)
    return False


def _bus_poll(limit=30):
    """Poll bus messages."""
    try:
        r = requests.get(BUS_MESSAGES, params={"limit": limit}, timeout=5)
        if r.ok:
            return r.json() if isinstance(r.json(), list) else []
    except Exception:
        pass
    return []


class GateMonitor:
    """Monitors bus and enforces convene-first protocol."""

    def __init__(self):
        from skynet_convene import ConveneGate
        self.gate = ConveneGate()
        self.seen_ids = set()
        self.intercepted = 0
        self.passed = 0

    def scan_once(self):
        """Scan bus for worker->orchestrator messages that should be gated."""
        msgs = _bus_poll(50)
        actions = []

        for m in msgs:
            mid = m.get("id", "")
            if mid in self.seen_ids:
                continue
            self.seen_ids.add(mid)

            sender = m.get("sender", "")
            topic = m.get("topic", "")
            msg_type = m.get("type", "")
            content = m.get("content", "")

            # Only intercept worker->orchestrator messages
            if topic != "orchestrator":
                continue
            if sender not in WORKER_NAMES:
                continue
            # Urgent bypasses gate
            if msg_type == "urgent":
                self.passed += 1
                actions.append({"action": "bypass", "sender": sender, "reason": "urgent"})
                continue
            # Already gated (from convene-gate sender)
            if sender == "convene-gate":
                continue
            # Gate-proposal votes are internal
            if msg_type in ("gate-proposal", "gate-vote"):
                continue

            # This is a direct worker->orchestrator report -- intercept!
            self.intercepted += 1
            result = self.gate.propose(sender, content)
            actions.append({
                "action": "intercepted",
                "sender": sender,
                "gate_id": result.get("gate_id"),
                "content_preview": content[:80],
            })

        # Expire stale proposals
        expired = self.gate.expire_stale(300)
        if expired:
            actions.append({"action": "expired", "count": len(expired)})

        return actions

    def run(self, interval=5, max_cycles=None):
        """Run the gate monitor loop."""
        print(f"[ConveneGate] Monitor started (poll every {interval}s)")
        print(f"[ConveneGate] Rule: Workers must get {self.gate.MAJORITY_THRESHOLD}+ votes before reaching orchestrator")
        cycle = 0
        try:
            while max_cycles is None or cycle < max_cycles:
                actions = self.scan_once()
                for a in actions:
                    if a["action"] == "intercepted":
                        print(f"  GATE: Intercepted {a['sender']} -> orchestrator. Gate ID: {a['gate_id']}")
                        print(f"        Preview: {a['content_preview']}")
                    elif a["action"] == "bypass":
                        print(f"  PASS: {a['sender']} bypassed (urgent)")
                    elif a["action"] == "expired":
                        print(f"  EXPIRE: {a['count']} stale proposals expired")
                time.sleep(interval)
                cycle += 1
        except KeyboardInterrupt:
            print("\n[ConveneGate] Monitor stopped")
        print(f"[ConveneGate] Summary: intercepted={self.intercepted} passed={self.passed}")


def propose_report(worker, report, urgent=False):
    """Worker proposes a report through the gate."""
    from skynet_convene import ConveneGate
    gate = ConveneGate()
    result = gate.propose(worker, report, urgent=urgent)
    return result


def vote_on_gate(gate_id, worker, approve=True):
    """Vote on a pending gate proposal."""
    from skynet_convene import ConveneGate
    gate = ConveneGate()
    result = gate.vote_gate(gate_id, worker, approve)
    return result


def show_pending():
    """Show all pending gate proposals."""
    from skynet_convene import ConveneGate
    gate = ConveneGate()
    pending = gate.get_pending()
    if not pending:
        print("No pending proposals")
        return
    for gid, p in pending.items():
        votes = p.get("votes", {})
        yes = sum(1 for v in votes.values() if v == "YES")
        age = int(time.time() - p.get("created_at", 0))
        print(f"  {gid}")
        print(f"    Proposer: {p['proposer']}")
        print(f"    Report:   {p['report'][:100]}")
        print(f"    Votes:    {yes}/{ConveneGate.MAJORITY_THRESHOLD} needed  ({', '.join(f'{w}={v}' for w,v in votes.items())})")
        print(f"    Age:      {age}s")
        print()


def show_stats():
    """Show gate statistics."""
    from skynet_convene import ConveneGate
    gate = ConveneGate()
    stats = gate.get_stats()
    print("ConveneGate Statistics:")
    print(f"  Total proposed:  {stats.get('total_proposed', 0)}")
    print(f"  Total elevated:  {stats.get('total_elevated', 0)}")
    print(f"  Total rejected:  {stats.get('total_rejected', 0)}")
    print(f"  Total bypassed:  {stats.get('total_bypassed', 0)}")


def run_protocol_test():
    """Simulate the convene-first protocol end-to-end."""
    from skynet_convene import ConveneGate
    gate = ConveneGate()

    print("=" * 60)
    print("CONVENE-FIRST PROTOCOL SIMULATION")
    print("=" * 60)

    # Test 1: Normal report (needs consensus)
    print("\n[TEST 1] Alpha wants to report a bug to orchestrator")
    r1 = gate.propose("alpha", "Found critical bug in auth module -- session tokens not rotated")
    gate_id = r1.get("gate_id")
    print(f"  Result: {r1['action']} (gate_id={gate_id}, votes={r1.get('votes',0)}/{r1.get('needed',2)})")
    assert r1["action"] == "proposed", "Should be pending"

    # Check -- not yet elevated
    pending = gate.get_pending()
    assert gate_id in pending, "Should be in pending"
    print(f"  Status: PENDING (need {r1.get('needed',2)} votes)")

    # Test 2: Beta agrees
    print("\n[TEST 2] Beta agrees with alpha's report")
    r2 = gate.vote_gate(gate_id, "beta", approve=True)
    print(f"  Result: {r2['action']}")
    if r2["action"] == "elevated":
        print(f"  ELEVATED to orchestrator! Voters: {r2.get('voters', [])}")
    else:
        print(f"  Votes: {r2.get('yes',0)}/{gate.MAJORITY_THRESHOLD} needed")

    # Verify elevated
    assert r2["action"] == "elevated", "Should be elevated after 2 YES votes"
    print("  PASS: Consensus reached, report delivered to orchestrator")

    # Test 3: Urgent bypass
    print("\n[TEST 3] Gamma sends urgent report (should bypass)")
    r3 = gate.propose("gamma", "SYSTEM DOWN -- immediate attention needed", urgent=True)
    print(f"  Result: {r3['action']} (delivered={r3.get('delivered', False)})")
    assert r3["action"] == "bypassed", "Urgent should bypass"
    print("  PASS: Urgent report bypassed gate and went directly to orchestrator")

    # Test 4: Rejected report
    print("\n[TEST 4] Delta proposes, alpha and beta reject")
    r4 = gate.propose("delta", "Suggest renaming variables for consistency")
    gate_id_4 = r4.get("gate_id")
    print(f"  Proposed: gate_id={gate_id_4}")
    r4a = gate.vote_gate(gate_id_4, "alpha", approve=False)
    print(f"  Alpha votes NO: {r4a['action']}")
    if r4a["action"] != "rejected":
        r4b = gate.vote_gate(gate_id_4, "beta", approve=False)
        print(f"  Beta votes NO: {r4b['action']}")
        assert r4b["action"] == "rejected", "Should be rejected after 2 NO votes"
    print("  PASS: Report rejected, not sent to orchestrator")

    # Stats
    print("\n" + "=" * 60)
    stats = gate.get_stats()
    print(f"FINAL STATS:")
    print(f"  Proposed:  {stats.get('total_proposed', 0)}")
    print(f"  Elevated:  {stats.get('total_elevated', 0)}")
    print(f"  Rejected:  {stats.get('total_rejected', 0)}")
    print(f"  Bypassed:  {stats.get('total_bypassed', 0)}")
    print("=" * 60)

    # Verify bus messages
    print("\nChecking bus for consensus message...")
    msgs = _bus_poll(10)
    consensus_msgs = [m for m in msgs if m.get("sender") == "convene-gate"
                      and m.get("topic") == "orchestrator"]
    if consensus_msgs:
        print(f"  Found {len(consensus_msgs)} consensus message(s) on bus:")
        for m in consensus_msgs:
            print(f"    [{m.get('type')}] {m.get('content', '')[:100]}")
    else:
        print("  (consensus message may have been posted earlier)")

    urgent_msgs = [m for m in msgs if m.get("type") == "urgent"
                   and m.get("sender") == "gamma"]
    if urgent_msgs:
        print(f"  Found {len(urgent_msgs)} urgent bypass message(s)")

    print("\nALL TESTS PASSED")
    return True


def main():
    parser = argparse.ArgumentParser(description="Convene-First Communication Gate")
    parser.add_argument("--monitor", action="store_true", help="Run gate monitor daemon")
    parser.add_argument("--propose", type=str, help="Propose a report to orchestrator")
    parser.add_argument("--vote", type=str, metavar="GATE_ID", help="Vote YES on proposal")
    parser.add_argument("--reject", type=str, metavar="GATE_ID", help="Vote NO on proposal")
    parser.add_argument("--pending", action="store_true", help="Show pending proposals")
    parser.add_argument("--stats", action="store_true", help="Show gate statistics")
    parser.add_argument("--test", action="store_true", help="Run protocol simulation")
    parser.add_argument("--worker", type=str, default="beta", help="Worker name")
    parser.add_argument("--urgent", action="store_true", help="Mark report as urgent (bypass gate)")
    parser.add_argument("--interval", type=int, default=5, help="Monitor poll interval (seconds)")
    args = parser.parse_args()

    if args.test:
        success = run_protocol_test()
        sys.exit(0 if success else 1)
    elif args.monitor:
        monitor = GateMonitor()
        monitor.run(interval=args.interval)
    elif args.propose:
        result = propose_report(args.worker, args.propose, urgent=args.urgent)
        print(json.dumps(result, indent=2, default=str))
    elif args.vote:
        result = vote_on_gate(args.vote, args.worker, approve=True)
        print(json.dumps(result, indent=2, default=str))
    elif args.reject:
        result = vote_on_gate(args.reject, args.worker, approve=False)
        print(json.dumps(result, indent=2, default=str))
    elif args.pending:
        show_pending()
    elif args.stats:
        show_stats()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
