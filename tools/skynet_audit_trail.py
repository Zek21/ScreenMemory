"""Hash-chained immutable audit trail for Skynet operations.
# signed: delta

Every entry is cryptographically linked to its predecessor via SHA-256,
creating a blockchain-like tamper-evident log. If any entry is modified,
deleted, or reordered, verify_chain() detects the break.

Storage: data/audit_trail.jsonl  (append-only JSONL, one entry per line)

Entry schema:
    {
        "seq": int,              # monotonic sequence number
        "timestamp": str,        # ISO-8601 UTC
        "agent": str,            # who performed the action
        "action": str,           # what was done
        "data": str|dict,        # payload / details
        "prev_hash": str,        # SHA-256 of previous entry (genesis = "0"*64)
        "entry_hash": str        # SHA-256(seq|timestamp|agent|action|data|prev_hash)
    }

Usage:
    python tools/skynet_audit_trail.py append <agent> <action> [--data DATA]
    python tools/skynet_audit_trail.py verify
    python tools/skynet_audit_trail.py query [--agent NAME] [--action ACT] [--since ISO]
    python tools/skynet_audit_trail.py tail [--n N]
    python tools/skynet_audit_trail.py stats

Python API:
    from tools.skynet_audit_trail import append_entry, verify_chain, query
    append_entry("alpha", "task_complete", {"task": "fix bug", "files": ["x.py"]})
    ok, errors = verify_chain()
    results = query(agent="alpha", since="2026-03-15T00:00:00")
"""
# signed: delta

import hashlib
import json
import os
import sys
import argparse
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

REPO_ROOT = Path(__file__).resolve().parent.parent
TRAIL_PATH = REPO_ROOT / "data" / "audit_trail.jsonl"
GENESIS_HASH = "0" * 64

_write_lock = threading.Lock()


def _compute_hash(seq: int, timestamp: str, agent: str, action: str,
                  data: str, prev_hash: str) -> str:
    """Compute SHA-256 over the canonical representation of an entry's fields."""
    # Canonical: pipe-separated, deterministic ordering  # signed: delta
    payload = f"{seq}|{timestamp}|{agent}|{action}|{data}|{prev_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _serialize_data(data: Any) -> str:
    """Normalize data field to a stable string for hashing."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    # For dicts/lists, use sorted-keys JSON for deterministic output
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def _read_last_entry() -> Optional[Dict]:
    """Read the last entry from the trail file without loading the full file."""
    if not TRAIL_PATH.exists() or TRAIL_PATH.stat().st_size == 0:
        return None

    # Read last non-empty line efficiently  # signed: delta
    last_line = ""
    with open(TRAIL_PATH, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                last_line = stripped

    if not last_line:
        return None

    try:
        return json.loads(last_line)
    except json.JSONDecodeError:
        return None


def _read_all_entries() -> List[Dict]:
    """Read every entry from the trail file."""
    if not TRAIL_PATH.exists():
        return []

    entries = []
    with open(TRAIL_PATH, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entries.append(json.loads(stripped))
            except json.JSONDecodeError:
                entries.append({"_parse_error": True, "_line": lineno, "_raw": stripped})
    return entries


def append_entry(agent: str, action: str, data: Any = None) -> Dict:
    """Append a new hash-chained entry to the audit trail.

    Args:
        agent:  Identity of the actor (e.g. "alpha", "orchestrator", "system").
        action: Short verb describing what happened (e.g. "task_dispatch",
                "file_edit", "bus_publish", "boot_complete").
        data:   Arbitrary payload — string, dict, or list.  Serialized to
                deterministic JSON for hashing.

    Returns:
        The complete entry dict that was written (including entry_hash).

    Thread-safe: uses a module-level lock so concurrent callers within the
    same process are serialized.  Cross-process safety relies on JSONL
    append semantics (atomic on most filesystems for < 4KB lines).
    """
    data_str = _serialize_data(data)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    with _write_lock:
        last = _read_last_entry()
        if last is None:
            seq = 0
            prev_hash = GENESIS_HASH
        else:
            seq = last.get("seq", 0) + 1
            prev_hash = last.get("entry_hash", GENESIS_HASH)

        entry_hash = _compute_hash(seq, timestamp, agent, action, data_str, prev_hash)

        entry = {
            "seq": seq,
            "timestamp": timestamp,
            "agent": agent,
            "action": action,
            "data": data if not isinstance(data, str) or data else data_str,
            "prev_hash": prev_hash,
            "entry_hash": entry_hash,
        }

        # Ensure data/ directory exists
        TRAIL_PATH.parent.mkdir(parents=True, exist_ok=True)

        # Append atomically (single write call)  # signed: delta
        with open(TRAIL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return entry


def verify_chain() -> Tuple[bool, List[str]]:
    """Verify the integrity of the entire audit trail.

    Checks:
        1. Genesis entry has prev_hash == GENESIS_HASH
        2. Every entry's entry_hash matches recomputed SHA-256
        3. Every entry's prev_hash matches predecessor's entry_hash
        4. Sequence numbers are monotonically increasing

    Returns:
        (is_valid, errors) — True with empty list if chain is intact,
        False with list of human-readable error descriptions if tampered.
    """
    entries = _read_all_entries()
    errors: List[str] = []

    if not entries:
        return True, []  # Empty chain is trivially valid

    for i, entry in enumerate(entries):
        # Parse errors from _read_all_entries
        if entry.get("_parse_error"):
            errors.append(f"Line {entry['_line']}: JSON parse error")
            continue

        seq = entry.get("seq", -1)
        timestamp = entry.get("timestamp", "")
        agent = entry.get("agent", "")
        action = entry.get("action", "")
        data_str = _serialize_data(entry.get("data"))
        prev_hash = entry.get("prev_hash", "")
        stored_hash = entry.get("entry_hash", "")

        # Check 1: recompute hash  # signed: delta
        expected_hash = _compute_hash(seq, timestamp, agent, action, data_str, prev_hash)
        if stored_hash != expected_hash:
            errors.append(
                f"Entry seq={seq}: hash mismatch — stored {stored_hash[:16]}... "
                f"!= computed {expected_hash[:16]}... (TAMPERED)"
            )

        # Check 2: genesis entry
        if i == 0:
            if prev_hash != GENESIS_HASH:
                errors.append(
                    f"Entry seq={seq}: genesis prev_hash is not all-zeros "
                    f"(got {prev_hash[:16]}...)"
                )
        else:
            # Check 3: chain linkage
            prev_entry = entries[i - 1]
            if not prev_entry.get("_parse_error"):
                expected_prev = prev_entry.get("entry_hash", "")
                if prev_hash != expected_prev:
                    errors.append(
                        f"Entry seq={seq}: prev_hash {prev_hash[:16]}... "
                        f"!= predecessor hash {expected_prev[:16]}... (CHAIN BREAK)"
                    )

            # Check 4: sequence monotonicity
            prev_seq = prev_entry.get("seq", -1) if not prev_entry.get("_parse_error") else -1
            if prev_seq >= 0 and seq != prev_seq + 1:
                errors.append(
                    f"Entry seq={seq}: expected seq={prev_seq + 1} (SEQUENCE GAP)"
                )

    return len(errors) == 0, errors


def query(
    agent: Optional[str] = None,
    action: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
) -> List[Dict]:
    """Search the audit trail with optional filters.

    Args:
        agent:  Filter by agent name (exact match, case-insensitive).
        action: Filter by action (substring match, case-insensitive).
        since:  ISO-8601 timestamp — return only entries at or after this time.
        limit:  Maximum entries to return (default 100, 0 = unlimited).

    Returns:
        List of matching entry dicts, newest first.
    """
    entries = _read_all_entries()
    results: List[Dict] = []

    agent_lower = agent.lower() if agent else None
    action_lower = action.lower() if action else None

    for entry in entries:
        if entry.get("_parse_error"):
            continue

        if agent_lower and entry.get("agent", "").lower() != agent_lower:
            continue

        if action_lower and action_lower not in entry.get("action", "").lower():
            continue

        if since:
            entry_ts = entry.get("timestamp", "")
            if entry_ts < since:
                continue

        results.append(entry)

    # Newest first
    results.reverse()

    if limit > 0:
        results = results[:limit]

    return results


def tail(n: int = 10) -> List[Dict]:
    """Return the last N entries from the trail."""
    entries = _read_all_entries()
    return entries[-n:] if entries else []


def stats() -> Dict[str, Any]:
    """Summary statistics for the audit trail."""
    entries = _read_all_entries()
    if not entries:
        return {"total_entries": 0, "chain_valid": True}

    valid_entries = [e for e in entries if not e.get("_parse_error")]
    agents = {}
    actions = {}
    for e in valid_entries:
        a = e.get("agent", "unknown")
        agents[a] = agents.get(a, 0) + 1
        act = e.get("action", "unknown")
        actions[act] = actions.get(act, 0) + 1

    is_valid, errs = verify_chain()

    return {
        "total_entries": len(entries),
        "valid_entries": len(valid_entries),
        "chain_valid": is_valid,
        "chain_errors": len(errs),
        "agents": agents,
        "actions": dict(sorted(actions.items(), key=lambda x: x[1], reverse=True)),
        "first_entry": valid_entries[0].get("timestamp", "") if valid_entries else "",
        "last_entry": valid_entries[-1].get("timestamp", "") if valid_entries else "",
    }  # signed: delta


def _cli():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Skynet hash-chained audit trail"
    )
    sub = parser.add_subparsers(dest="command")

    # append
    ap = sub.add_parser("append", help="Append a new entry")
    ap.add_argument("agent", help="Agent name")
    ap.add_argument("action", help="Action performed")
    ap.add_argument("--data", default=None, help="Data payload (string or JSON)")

    # verify
    sub.add_parser("verify", help="Verify chain integrity")

    # query
    qp = sub.add_parser("query", help="Search entries")
    qp.add_argument("--agent", default=None, help="Filter by agent")
    qp.add_argument("--action", default=None, help="Filter by action (substring)")
    qp.add_argument("--since", default=None, help="ISO-8601 timestamp lower bound")
    qp.add_argument("--limit", type=int, default=20, help="Max results (default 20)")

    # tail
    tp = sub.add_parser("tail", help="Show last N entries")
    tp.add_argument("--n", type=int, default=10, help="Number of entries")

    # stats
    sub.add_parser("stats", help="Show trail statistics")

    args = parser.parse_args()

    if args.command == "append":
        data = args.data
        if data:
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                pass  # Keep as string
        entry = append_entry(args.agent, args.action, data)
        print(f"Appended entry seq={entry['seq']} hash={entry['entry_hash'][:16]}...")

    elif args.command == "verify":
        is_valid, errors = verify_chain()
        if is_valid:
            entries = _read_all_entries()
            print(f"CHAIN VALID — {len(entries)} entries, no tampering detected.")
        else:
            print(f"CHAIN BROKEN — {len(errors)} error(s):")
            for err in errors:
                print(f"  ✗ {err}")
            sys.exit(1)

    elif args.command == "query":
        results = query(
            agent=args.agent,
            action=args.action,
            since=args.since,
            limit=args.limit,
        )
        if not results:
            print("No matching entries.")
        else:
            print(f"Found {len(results)} entries:")
            for e in results:
                ts = e.get("timestamp", "?")[:19]
                print(
                    f"  [{e.get('seq', '?')}] {ts} {e.get('agent', '?')} "
                    f"| {e.get('action', '?')} | {_serialize_data(e.get('data', ''))[:80]}"
                )

    elif args.command == "tail":
        entries = tail(args.n)
        if not entries:
            print("Audit trail is empty.")
        else:
            for e in entries:
                ts = e.get("timestamp", "?")[:19]
                print(
                    f"  [{e.get('seq', '?')}] {ts} {e.get('agent', '?')} "
                    f"| {e.get('action', '?')} | hash={e.get('entry_hash', '?')[:12]}..."
                )

    elif args.command == "stats":
        s = stats()
        print(f"Total entries:  {s['total_entries']}")
        print(f"Chain valid:    {'YES' if s['chain_valid'] else 'NO (' + str(s['chain_errors']) + ' errors)'}")
        if s.get("first_entry"):
            print(f"First entry:    {s['first_entry'][:19]}")
            print(f"Last entry:     {s['last_entry'][:19]}")
        if s.get("agents"):
            print("Agents:")
            for a, c in sorted(s["agents"].items(), key=lambda x: x[1], reverse=True):
                print(f"  {a}: {c}")
        if s.get("actions"):
            print("Top actions:")
            for act, c in list(s["actions"].items())[:10]:
                print(f"  {act}: {c}")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: delta
