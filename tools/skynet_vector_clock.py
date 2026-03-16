"""Skynet Vector Clocks for Causal Ordering — P3.07

Lamport-style vector clocks providing causal ordering of messages across
the Skynet multi-agent system.  Each agent maintains a logical timestamp
counter; merging two clocks on message receive captures the "happens-before"
relation.  CausalMessageStore keeps an ordered log that can be queried in
causal order and can detect truly concurrent events (neither happens-before
the other).

Usage:
    python tools/skynet_vector_clock.py tick AGENT
    python tools/skynet_vector_clock.py merge AGENT OTHER_AGENT
    python tools/skynet_vector_clock.py history [--limit N]
    python tools/skynet_vector_clock.py concurrent [--limit N]
    python tools/skynet_vector_clock.py status
    python tools/skynet_vector_clock.py reset

Python API:
    from tools.skynet_vector_clock import VectorClock, CausalMessageStore
    vc = VectorClock("alpha")
    vc.increment()
    store = CausalMessageStore()
    store.record(sender="alpha", content="hello", clock=vc)
    ordered = store.query_causally_ordered()
    concurrent = store.find_concurrent_pairs(limit=10)
"""
# signed: delta

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

DATA_DIR = _REPO / "data"
CLOCK_STATE_FILE = DATA_DIR / "vector_clocks.json"
CAUSAL_STORE_FILE = DATA_DIR / "causal_messages.jsonl"

# All known Skynet agents for vector-clock dimensionality
ALL_AGENTS = [
    "orchestrator", "alpha", "beta", "gamma", "delta",
    "consultant", "gemini_consultant", "system",
]

_lock = threading.Lock()


# ── VectorClock ─────────────────────────────────────────────────────
# signed: delta

class VectorClock:
    """Lamport vector clock for a single agent.

    The clock is a dict mapping agent names to integer logical timestamps.
    Missing agents are implicitly 0.

    Properties
    ----------
    - increment(agent) bumps that agent's counter by 1.
    - merge(other) takes element-wise max, then increments self.
    - happens_before(other) returns True if self < other (strict partial order).
    - is_concurrent(other) returns True when neither dominates.

    Thread Safety
    -------------
    Operations on a single VectorClock instance are NOT thread-safe.
    External locking is needed for multi-threaded use.
    """

    __slots__ = ("owner", "_clocks")

    def __init__(self, owner: str, clocks: Optional[Dict[str, int]] = None):
        self.owner = owner
        self._clocks: Dict[str, int] = dict(clocks) if clocks else {}

    # ── Core operations ──────────────────────────────────────────

    def increment(self, agent: Optional[str] = None) -> "VectorClock":
        """Increment the logical timestamp for *agent* (defaults to owner).

        Returns self for chaining.
        """
        agent = agent or self.owner
        self._clocks[agent] = self._clocks.get(agent, 0) + 1
        return self

    def merge(self, other: "VectorClock") -> "VectorClock":
        """Merge another clock into this one (element-wise max).

        After merge, increments the owner's own counter to capture
        the receive event.  Returns self for chaining.
        """
        all_keys = set(self._clocks) | set(other._clocks)
        for k in all_keys:
            self._clocks[k] = max(
                self._clocks.get(k, 0), other._clocks.get(k, 0)
            )
        # Receiving a message is an event → tick own counter
        self._clocks[self.owner] = self._clocks.get(self.owner, 0) + 1
        return self

    # ── Ordering relations ───────────────────────────────────────

    def happens_before(self, other: "VectorClock") -> bool:
        """Return True if self happens-before other (self < other).

        self < other iff:
          ∀ k: self[k] ≤ other[k]  AND  ∃ k: self[k] < other[k]
        """
        all_keys = set(self._clocks) | set(other._clocks)
        at_least_one_less = False
        for k in all_keys:
            s = self._clocks.get(k, 0)
            o = other._clocks.get(k, 0)
            if s > o:
                return False
            if s < o:
                at_least_one_less = True
        return at_least_one_less

    def is_concurrent(self, other: "VectorClock") -> bool:
        """Return True if neither clock dominates the other.

        Two events are concurrent iff ¬(self < other) ∧ ¬(other < self).
        """
        return (not self.happens_before(other)
                and not other.happens_before(self))

    # ── Comparison helpers ───────────────────────────────────────

    def dominates(self, other: "VectorClock") -> bool:
        """Return True if self ≥ other (self dominates or equals)."""
        all_keys = set(self._clocks) | set(other._clocks)
        return all(
            self._clocks.get(k, 0) >= other._clocks.get(k, 0)
            for k in all_keys
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VectorClock):
            return NotImplemented
        all_keys = set(self._clocks) | set(other._clocks)
        return all(
            self._clocks.get(k, 0) == other._clocks.get(k, 0)
            for k in all_keys
        )

    def __repr__(self) -> str:
        non_zero = {k: v for k, v in sorted(self._clocks.items()) if v > 0}
        return f"VectorClock({self.owner}, {non_zero})"

    # ── Scalar summary ───────────────────────────────────────────

    def total_order_key(self) -> Tuple[int, str]:
        """Return a tie-breaking key for total ordering.

        Primary: sum of all counters (Lamport-like scalar).
        Secondary: owner name (deterministic tie-break).
        """
        return (sum(self._clocks.values()), self.owner)

    # ── Serialization ────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {"owner": self.owner, "clocks": dict(self._clocks)}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "VectorClock":
        return cls(owner=d["owner"], clocks=d.get("clocks", {}))

    def copy(self) -> "VectorClock":
        return VectorClock(self.owner, dict(self._clocks))

    @property
    def clocks(self) -> Dict[str, int]:
        """Read-only view of the clock map."""
        return dict(self._clocks)


# ── Clock State Persistence ─────────────────────────────────────────
# signed: delta

def _load_clocks() -> Dict[str, Dict[str, Any]]:
    """Load all agent clocks from disk."""
    if not CLOCK_STATE_FILE.exists():
        return {}
    try:
        with open(CLOCK_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_clocks(data: Dict[str, Dict[str, Any]]) -> None:
    """Save all agent clocks atomically."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CLOCK_STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(CLOCK_STATE_FILE)


def get_clock(agent: str) -> VectorClock:
    """Load or create a VectorClock for the given agent."""
    with _lock:
        data = _load_clocks()
        if agent in data:
            return VectorClock.from_dict(data[agent])
        return VectorClock(agent)


def save_clock(vc: VectorClock) -> None:
    """Persist a VectorClock to disk."""
    with _lock:
        data = _load_clocks()
        data[vc.owner] = vc.to_dict()
        _save_clocks(data)


def tick(agent: str) -> VectorClock:
    """Increment agent's clock and persist. Returns updated clock."""
    with _lock:
        data = _load_clocks()
        if agent in data:
            vc = VectorClock.from_dict(data[agent])
        else:
            vc = VectorClock(agent)
        vc.increment()
        data[agent] = vc.to_dict()
        _save_clocks(data)
    return vc


def merge_clocks(agent: str, other_agent: str) -> VectorClock:
    """Merge other_agent's clock into agent's clock. Persists both."""
    with _lock:
        data = _load_clocks()
        vc_a = VectorClock.from_dict(data[agent]) if agent in data else VectorClock(agent)
        vc_b = VectorClock.from_dict(data[other_agent]) if other_agent in data else VectorClock(other_agent)
        vc_a.merge(vc_b)
        data[agent] = vc_a.to_dict()
        _save_clocks(data)
    return vc_a


# ── Bus Integration ─────────────────────────────────────────────────
# signed: delta

def attach_vector_clock(message: Dict[str, Any], sender: str) -> Dict[str, Any]:
    """Attach a vector clock snapshot to a bus message.

    Increments the sender's clock before attaching, so the message
    captures the "send" event.  Persists the updated clock.

    Args:
        message: The bus message dict (must have 'sender' field).
        sender:  Agent name performing the send.

    Returns:
        The message dict with ``_vector_clock`` added to metadata.
    """
    vc = tick(sender)  # increment + persist
    if "metadata" not in message:
        message["metadata"] = {}
    message["metadata"]["_vector_clock"] = vc.to_dict()
    return message


def extract_vector_clock(message: Dict[str, Any]) -> Optional[VectorClock]:
    """Extract a VectorClock from a bus message, if present."""
    meta = message.get("metadata", {})
    vc_data = meta.get("_vector_clock")
    if vc_data and isinstance(vc_data, dict):
        return VectorClock.from_dict(vc_data)
    return None


def receive_and_merge(message: Dict[str, Any], receiver: str) -> VectorClock:
    """Process a received message: merge its vector clock into receiver's.

    Merges the message's embedded clock (not the sender's persisted clock)
    into the receiver's clock.  This correctly captures the causal
    relationship at the time the message was sent.

    If the message has no vector clock, just increments receiver's clock.
    Persists the updated clock.
    """
    msg_vc = extract_vector_clock(message)
    if msg_vc:
        with _lock:
            data = _load_clocks()
            vc_recv = VectorClock.from_dict(data[receiver]) if receiver in data else VectorClock(receiver)
            vc_recv.merge(msg_vc)
            data[receiver] = vc_recv.to_dict()
            _save_clocks(data)
        return vc_recv
    else:
        return tick(receiver)
    # signed: delta


# ── CausalMessageStore ──────────────────────────────────────────────
# signed: delta

@dataclass
class CausalMessage:
    """A message annotated with a vector clock for causal ordering."""
    msg_id: str
    sender: str
    content: str
    topic: str
    msg_type: str
    timestamp: float  # wall-clock for display only
    vector_clock: Dict[str, int]  # serialized clock map
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "sender": self.sender,
            "content": self.content,
            "topic": self.topic,
            "type": self.msg_type,
            "timestamp": self.timestamp,
            "vector_clock": self.vector_clock,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CausalMessage":
        return cls(
            msg_id=d.get("msg_id", str(uuid.uuid4())[:8]),
            sender=d.get("sender", "unknown"),
            content=d.get("content", ""),
            topic=d.get("topic", ""),
            msg_type=d.get("type", ""),
            timestamp=d.get("timestamp", 0.0),
            vector_clock=d.get("vector_clock", {}),
        )


class CausalMessageStore:
    """Stores messages with vector clocks and provides causal queries.

    Messages are persisted to a JSONL file for durability.
    """

    def __init__(self, path: Optional[Path] = None):
        self._path = path or CAUSAL_STORE_FILE
        self._messages: List[CausalMessage] = []
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._messages = self._load()
        self._loaded = True

    def _load(self) -> List[CausalMessage]:
        if not self._path.exists():
            return []
        msgs: List[CausalMessage] = []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msgs.append(CausalMessage.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, KeyError):
                        continue
        except OSError:
            pass
        return msgs

    def _append_to_file(self, msg: CausalMessage) -> None:
        """Append a single message to the JSONL store."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")

    def record(self, sender: str, content: str, clock: VectorClock,
               topic: str = "", msg_type: str = "",
               msg_id: Optional[str] = None) -> CausalMessage:
        """Record a message with its vector clock snapshot.

        Args:
            sender:   Agent that sent the message.
            content:  Message content string.
            clock:    VectorClock snapshot at send time.
            topic:    Bus topic (optional).
            msg_type: Message type (optional).
            msg_id:   Explicit ID (auto-generated if None).

        Returns:
            The recorded CausalMessage.
        """
        self._ensure_loaded()
        msg = CausalMessage(
            msg_id=msg_id or str(uuid.uuid4())[:12],
            sender=sender,
            content=content,
            topic=topic,
            msg_type=msg_type,
            timestamp=time.time(),
            vector_clock=clock.clocks,  # snapshot
        )
        self._messages.append(msg)
        self._append_to_file(msg)
        return msg

    def record_from_bus(self, bus_message: Dict[str, Any]) -> Optional[CausalMessage]:
        """Record a bus message that already has a _vector_clock in metadata."""
        vc = extract_vector_clock(bus_message)
        if not vc:
            return None
        return self.record(
            sender=bus_message.get("sender", "unknown"),
            content=bus_message.get("content", ""),
            clock=vc,
            topic=bus_message.get("topic", ""),
            msg_type=bus_message.get("type", ""),
        )

    # ── Causal Ordering ──────────────────────────────────────────

    def query_causally_ordered(self,
                               limit: int = 100,
                               sender: Optional[str] = None,
                               topic: Optional[str] = None) -> List[CausalMessage]:
        """Return messages sorted in causal order.

        Uses topological sort based on the happens-before relation.
        Ties (concurrent events) are broken by wall-clock timestamp
        then by sender name for determinism.

        Args:
            limit:  Max messages to return.
            sender: Filter by sender (optional).
            topic:  Filter by topic (optional).

        Returns:
            List of CausalMessage in causal order.
        """
        self._ensure_loaded()
        msgs = list(self._messages)

        # Apply filters
        if sender:
            msgs = [m for m in msgs if m.sender == sender]
        if topic:
            msgs = [m for m in msgs if m.topic == topic]

        if not msgs:
            return []

        # Build causal DAG — msgs[i] → msgs[j] if i happens-before j
        n = len(msgs)
        # For large sets, limit pairwise comparisons
        if n > 500:
            msgs = msgs[-500:]
            n = len(msgs)

        clocks = [VectorClock(m.sender, m.vector_clock) for m in msgs]
        # Kahn's topological sort
        in_degree = [0] * n
        adj: List[List[int]] = [[] for _ in range(n)]

        for i in range(n):
            for j in range(i + 1, n):
                if clocks[i].happens_before(clocks[j]):
                    adj[i].append(j)
                    in_degree[j] += 1
                elif clocks[j].happens_before(clocks[i]):
                    adj[j].append(i)
                    in_degree[i] += 1
                # concurrent: no edge

        # BFS topological sort with tie-breaking
        from collections import deque
        queue: List[int] = []
        for i in range(n):
            if in_degree[i] == 0:
                queue.append(i)
        # Sort initial queue by (timestamp, sender) for deterministic tie-break
        queue.sort(key=lambda i: (msgs[i].timestamp, msgs[i].sender))

        result: List[CausalMessage] = []
        visited = set()
        while queue and len(result) < limit:
            # Pick the earliest (by wall-clock) among ready nodes
            queue.sort(key=lambda i: (msgs[i].timestamp, msgs[i].sender))
            idx = queue.pop(0)
            if idx in visited:
                continue
            visited.add(idx)
            result.append(msgs[idx])
            for j in adj[idx]:
                in_degree[j] -= 1
                if in_degree[j] == 0 and j not in visited:
                    queue.append(j)

        # Catch any unvisited (cycles shouldn't happen with vector clocks, but be safe)
        if len(result) < limit:
            for i in range(n):
                if i not in visited and len(result) < limit:
                    result.append(msgs[i])

        return result[:limit]

    # ── Concurrent Event Detection ───────────────────────────────

    def find_concurrent_pairs(self,
                              limit: int = 20,
                              window_s: float = 0.0) -> List[Tuple[CausalMessage, CausalMessage]]:
        """Find pairs of messages that are causally concurrent.

        Two messages are concurrent if neither happens-before the other.

        Args:
            limit:    Max pairs to return.
            window_s: If > 0, only consider messages within this wall-clock
                      window of each other.

        Returns:
            List of (msg_a, msg_b) concurrent pairs.
        """
        self._ensure_loaded()
        msgs = list(self._messages)
        # Only scan recent messages for performance
        if len(msgs) > 200:
            msgs = msgs[-200:]

        pairs: List[Tuple[CausalMessage, CausalMessage]] = []
        n = len(msgs)
        for i in range(n):
            if len(pairs) >= limit:
                break
            vc_i = VectorClock(msgs[i].sender, msgs[i].vector_clock)
            for j in range(i + 1, n):
                if len(pairs) >= limit:
                    break
                if window_s > 0 and abs(msgs[i].timestamp - msgs[j].timestamp) > window_s:
                    continue
                vc_j = VectorClock(msgs[j].sender, msgs[j].vector_clock)
                if vc_i.is_concurrent(vc_j):
                    pairs.append((msgs[i], msgs[j]))

        return pairs

    # ── Query Helpers ────────────────────────────────────────────

    def get_all(self, limit: int = 100) -> List[CausalMessage]:
        """Return the last *limit* messages in insertion order."""
        self._ensure_loaded()
        return list(self._messages[-limit:])

    def count(self) -> int:
        self._ensure_loaded()
        return len(self._messages)

    def clear(self) -> None:
        """Clear all stored messages."""
        self._messages = []
        self._loaded = True
        if self._path.exists():
            self._path.unlink()

    # ── Metrics ──────────────────────────────────────────────────

    def metrics(self) -> Dict[str, Any]:
        """Return causal store metrics."""
        self._ensure_loaded()
        msgs = self._messages
        senders: Dict[str, int] = {}
        for m in msgs:
            senders[m.sender] = senders.get(m.sender, 0) + 1
        concurrent_count = len(self.find_concurrent_pairs(limit=50))
        return {
            "total_messages": len(msgs),
            "unique_senders": len(senders),
            "messages_per_sender": senders,
            "concurrent_pairs_found": concurrent_count,
        }


# ── Convenience Functions ────────────────────────────────────────────
# signed: delta

def causal_publish(message: Dict[str, Any], sender: str,
                   store: Optional[CausalMessageStore] = None) -> Dict[str, Any]:
    """Attach vector clock to a message and optionally record it.

    This is the integration point: call this before guarded_publish()
    to add causal ordering metadata to every bus message.

    Args:
        message: Bus message dict.
        sender:  Agent sending the message.
        store:   Optional CausalMessageStore to record the message.

    Returns:
        The message with vector clock attached.
    """
    msg = attach_vector_clock(message, sender)
    if store:
        vc = extract_vector_clock(msg)
        if vc:
            store.record(
                sender=sender,
                content=message.get("content", ""),
                clock=vc,
                topic=message.get("topic", ""),
                msg_type=message.get("type", ""),
            )
    return msg


def causal_receive(message: Dict[str, Any], receiver: str,
                   store: Optional[CausalMessageStore] = None) -> VectorClock:
    """Process a received message: merge clock and optionally record.

    Args:
        message:  Bus message dict with vector clock metadata.
        receiver: Agent receiving the message.
        store:    Optional CausalMessageStore to record.

    Returns:
        Updated VectorClock for the receiver.
    """
    vc = receive_and_merge(message, receiver)
    if store:
        msg_vc = extract_vector_clock(message)
        if msg_vc:
            store.record(
                sender=message.get("sender", "unknown"),
                content=message.get("content", ""),
                clock=msg_vc,
                topic=message.get("topic", ""),
                msg_type=message.get("type", ""),
            )
    return vc


# ── CLI ──────────────────────────────────────────────────────────────
# signed: delta

def _cli() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Skynet Vector Clocks for Causal Ordering -- P3.07",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/skynet_vector_clock.py tick alpha
  python tools/skynet_vector_clock.py merge alpha beta
  python tools/skynet_vector_clock.py history --limit 20
  python tools/skynet_vector_clock.py concurrent --limit 10
  python tools/skynet_vector_clock.py status
  python tools/skynet_vector_clock.py reset
""",
    )
    sub = parser.add_subparsers(dest="command")

    # tick
    tick_p = sub.add_parser("tick", help="Increment an agent's vector clock")
    tick_p.add_argument("agent", help="Agent name (e.g. alpha)")

    # merge
    merge_p = sub.add_parser("merge",
                             help="Merge another agent's clock into yours")
    merge_p.add_argument("agent", help="Agent receiving the merge")
    merge_p.add_argument("other", help="Agent whose clock to merge in")

    # history
    hist_p = sub.add_parser("history",
                            help="Show causally ordered message history")
    hist_p.add_argument("--limit", "-l", type=int, default=20)
    hist_p.add_argument("--sender", "-s", help="Filter by sender")
    hist_p.add_argument("--topic", "-t", help="Filter by topic")

    # concurrent
    conc_p = sub.add_parser("concurrent",
                            help="Find concurrent (unordered) message pairs")
    conc_p.add_argument("--limit", "-l", type=int, default=10)
    conc_p.add_argument("--window", "-w", type=float, default=0.0,
                        help="Wall-clock window in seconds (0=no limit)")

    # status
    sub.add_parser("status", help="Show current vector clock state")

    # reset
    sub.add_parser("reset", help="Reset all clocks and causal store")

    args = parser.parse_args()

    if args.command == "tick":
        vc = tick(args.agent)
        print(f"Ticked {args.agent}: {vc.clocks}")

    elif args.command == "merge":
        vc = merge_clocks(args.agent, args.other)
        print(f"Merged {args.other} into {args.agent}: {vc.clocks}")

    elif args.command == "history":
        store = CausalMessageStore()
        ordered = store.query_causally_ordered(
            limit=args.limit, sender=args.sender, topic=args.topic,
        )
        if not ordered:
            print("No causal messages recorded.")
            return
        print(f"Causal Message History ({len(ordered)} messages)")
        print("=" * 80)
        for m in ordered:
            ts = time.strftime("%H:%M:%S", time.localtime(m.timestamp))
            vc_summary = {k: v for k, v in m.vector_clock.items() if v > 0}
            print(f"  {ts} | {m.sender:>10} | {m.msg_type:>10} | "
                  f"vc={vc_summary} | {m.content[:60]}")

    elif args.command == "concurrent":
        store = CausalMessageStore()
        pairs = store.find_concurrent_pairs(
            limit=args.limit, window_s=args.window,
        )
        if not pairs:
            print("No concurrent message pairs found.")
            return
        print(f"Concurrent Message Pairs ({len(pairs)} found)")
        print("=" * 80)
        for a, b in pairs:
            ts_a = time.strftime("%H:%M:%S", time.localtime(a.timestamp))
            ts_b = time.strftime("%H:%M:%S", time.localtime(b.timestamp))
            print(f"  {a.sender}@{ts_a} || {b.sender}@{ts_b}")
            print(f"    A: {a.content[:50]}")
            print(f"    B: {b.content[:50]}")
            print()

    elif args.command == "status":
        data = _load_clocks()
        if not data:
            print("No vector clocks initialized.")
            return
        print("Vector Clock Status")
        print("=" * 60)
        for agent, vc_data in sorted(data.items()):
            clocks = vc_data.get("clocks", {})
            non_zero = {k: v for k, v in sorted(clocks.items()) if v > 0}
            total = sum(clocks.values())
            print(f"  {agent:>20}: total={total:>5}  {non_zero}")

        store = CausalMessageStore()
        m = store.metrics()
        print()
        print(f"Causal Store: {m['total_messages']} messages, "
              f"{m['unique_senders']} senders, "
              f"{m['concurrent_pairs_found']} concurrent pairs")

    elif args.command == "reset":
        if CLOCK_STATE_FILE.exists():
            CLOCK_STATE_FILE.unlink()
        if CAUSAL_STORE_FILE.exists():
            CAUSAL_STORE_FILE.unlink()
        print("All vector clocks and causal store reset.")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: delta
