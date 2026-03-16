"""Skynet Message Ordering — Sequence numbers, gap detection, and reordering.

Wraps the Skynet bus publish/consume path with per-sender sequence numbers
so consumers can detect missing messages and reorder out-of-order arrivals.

Usage:
    # Publish with sequence number
    from tools.skynet_message_order import sequenced_publish
    sequenced_publish({"sender": "delta", "topic": "orchestrator",
                       "type": "result", "content": "hello"})

    # Consume with ordering guarantee
    from tools.skynet_message_order import ordered_consume
    ordered = ordered_consume(raw_messages, sender="delta")

    # CLI
    python tools/skynet_message_order.py stats
    python tools/skynet_message_order.py gaps
    python tools/skynet_message_order.py reset delta

# signed: delta
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Paths ──────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SEQ_FILE = DATA_DIR / "message_sequences.json"

_lock = threading.Lock()


# ── Sequence State ─────────────────────────────────────────────────

def _load_state() -> Dict[str, Any]:
    """Load persisted sequence state from disk."""
    if SEQ_FILE.exists():
        try:
            with open(SEQ_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "publishers": {},   # sender -> next_seq
        "consumers": {},    # sender -> last_consumed_seq
        "metrics": {
            "messages_ordered": 0,
            "gaps_detected": 0,
            "gaps_resolved": 0,
            "gaps_timed_out": 0,
            "max_gap_size": 0,
            "total_published": 0,
            "total_consumed": 0,
            "out_of_order_corrected": 0,
        },
        "updated_at": "",
    }


def _save_state(state: Dict[str, Any]) -> None:
    """Atomically persist sequence state."""
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = SEQ_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(str(tmp), str(SEQ_FILE))
    # signed: delta


# ── SequencedPublisher ─────────────────────────────────────────────

class SequencedPublisher:
    """Wraps guarded_publish with auto-incrementing per-sender sequence numbers.

    Each sender gets an independent monotonically increasing counter.
    The sequence number is injected into the message metadata before
    publishing, enabling consumers to detect gaps and reorder.

    Thread-safe: all operations hold a module-level lock.

    Example::

        pub = SequencedPublisher("delta")
        pub.publish({"topic": "orchestrator", "type": "result",
                     "content": "done"})
        # Message sent with metadata._seq = 1, metadata._sender_seq = "delta"
    # signed: delta
    """

    def __init__(self, sender: str):
        self.sender = sender
        self._ensure_sender()

    def _ensure_sender(self) -> None:
        """Register sender in state if not present."""
        with _lock:
            state = _load_state()
            if self.sender not in state["publishers"]:
                state["publishers"][self.sender] = 1
                _save_state(state)
        # signed: delta

    def next_seq(self) -> int:
        """Get and increment the next sequence number for this sender."""
        with _lock:
            state = _load_state()
            seq = state["publishers"].get(self.sender, 1)
            state["publishers"][self.sender] = seq + 1
            state["metrics"]["total_published"] = (
                state["metrics"].get("total_published", 0) + 1
            )
            _save_state(state)
        return seq
        # signed: delta

    def publish(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Publish a message with an injected sequence number.

        Injects ``_seq`` and ``_sender_seq`` into the message's
        ``metadata`` dict, then delegates to ``guarded_publish``.

        Args:
            msg: Message dict.  Must contain at least ``topic``,
                 ``type``, and ``content``.  ``sender`` is set to
                 ``self.sender`` if absent.

        Returns:
            The augmented message dict (with metadata injected).
        """
        seq = self.next_seq()

        msg.setdefault("sender", self.sender)
        metadata = msg.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["_seq"] = seq
        metadata["_sender_seq"] = self.sender
        metadata["_seq_ts"] = time.time()
        msg["metadata"] = metadata

        try:
            from tools.skynet_spam_guard import guarded_publish
            guarded_publish(msg)
        except ImportError:
            pass  # spam guard not available — sequence still tracked
        except Exception:
            pass  # publish failure — sequence still tracked

        return msg
        # signed: delta

    def current_seq(self) -> int:
        """Return the next sequence number WITHOUT incrementing."""
        with _lock:
            state = _load_state()
            return state["publishers"].get(self.sender, 1)

    def reset(self) -> None:
        """Reset this sender's sequence counter to 1."""
        with _lock:
            state = _load_state()
            state["publishers"][self.sender] = 1
            _save_state(state)
        # signed: delta


# ── OrderedConsumer ────────────────────────────────────────────────

@dataclass
class _BufferedMessage:
    """A message waiting for a gap to be filled."""
    seq: int
    msg: Dict[str, Any]
    buffered_at: float = field(default_factory=time.time)


class OrderedConsumer:
    """Consumes messages from a specific sender in sequence order.

    Detects gaps (missing sequence numbers) and buffers out-of-order
    messages until the gap is filled or a timeout expires.

    Args:
        sender:      The sender to track ordering for.
        gap_timeout: Seconds to wait for a missing message before
                     releasing buffered messages anyway (default 5.0).

    Example::

        consumer = OrderedConsumer("delta")
        ordered = consumer.consume(raw_messages)
        # ordered is a list of messages sorted by sequence number,
        # with gaps detected and metrics updated.
    # signed: delta
    """

    def __init__(self, sender: str, gap_timeout: float = 5.0):
        self.sender = sender
        self.gap_timeout = gap_timeout
        self._buffer: Dict[int, _BufferedMessage] = {}
        self._init_consumer_state()

    def _init_consumer_state(self) -> None:
        with _lock:
            state = _load_state()
            if self.sender not in state["consumers"]:
                state["consumers"][self.sender] = 0
                _save_state(state)
        # signed: delta

    @property
    def last_consumed(self) -> int:
        """Last sequence number successfully consumed."""
        with _lock:
            state = _load_state()
            return state["consumers"].get(self.sender, 0)

    def consume(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Process raw messages, returning them in sequence order.

        Steps:
          1. Filter messages for this sender that have sequence metadata.
          2. Sort by sequence number.
          3. Detect gaps — if seq N arrives but N-1 is missing, buffer N.
          4. Wait up to gap_timeout for missing messages.
          5. Release buffered messages after timeout (with gap logged).
          6. Return ordered list.

        Args:
            messages: Raw bus messages (may contain messages from
                      multiple senders; only this sender's are processed).

        Returns:
            List of messages in correct sequence order.
        """
        # Extract sequenced messages for this sender
        sequenced = self._extract_sequenced(messages)
        if not sequenced and not self._buffer:
            return []

        # Add new messages to internal buffer
        for seq, msg in sequenced:
            if seq not in self._buffer:
                self._buffer[seq] = _BufferedMessage(seq=seq, msg=msg)

        # Try to drain in order
        return self._drain_ordered()
        # signed: delta

    def _extract_sequenced(
        self, messages: List[Dict[str, Any]]
    ) -> List[Tuple[int, Dict[str, Any]]]:
        """Pull out messages for this sender with valid _seq metadata."""
        result = []
        for msg in messages:
            meta = msg.get("metadata", {})
            if not isinstance(meta, dict):
                continue
            sender_seq = meta.get("_sender_seq", "")
            seq = meta.get("_seq")
            if sender_seq != self.sender or seq is None:
                continue
            try:
                seq = int(seq)
            except (TypeError, ValueError):
                continue
            result.append((seq, msg))
        return result
        # signed: delta

    def _drain_ordered(self) -> List[Dict[str, Any]]:
        """Drain buffered messages in order, handling gaps.

        Returns messages that can be delivered (contiguous from
        last_consumed + 1, plus any timed-out buffered messages).
        """
        ordered: List[Dict[str, Any]] = []
        last = self.last_consumed
        now = time.time()

        gaps_detected = 0
        gaps_resolved = 0
        out_of_order = 0
        max_gap = 0

        # Phase 1: deliver contiguous messages starting from last+1
        while True:
            next_seq = last + 1
            if next_seq in self._buffer:
                entry = self._buffer.pop(next_seq)
                ordered.append(entry.msg)
                last = next_seq
            else:
                break

        # Phase 2: check for gaps
        if self._buffer:
            buffered_seqs = sorted(self._buffer.keys())
            expected_next = last + 1

            for seq in buffered_seqs:
                if seq > expected_next:
                    gap_size = seq - expected_next
                    gaps_detected += 1
                    max_gap = max(max_gap, gap_size)

                    entry = self._buffer[seq]
                    age = now - entry.buffered_at

                    if age >= self.gap_timeout:
                        # Timeout expired — release with gap logged
                        for release_seq in sorted(
                            s for s in self._buffer if s <= seq
                        ):
                            released = self._buffer.pop(release_seq)
                            ordered.append(released.msg)
                            last = release_seq
                            out_of_order += 1

                        gaps_resolved += 1  # resolved by timeout
                    # else: keep buffered, waiting for gap fill

                expected_next = seq + 1

        # Phase 3: update persistent state
        if ordered:
            with _lock:
                state = _load_state()
                state["consumers"][self.sender] = last
                m = state["metrics"]
                m["messages_ordered"] = m.get("messages_ordered", 0) + len(ordered)
                m["total_consumed"] = m.get("total_consumed", 0) + len(ordered)
                m["gaps_detected"] = m.get("gaps_detected", 0) + gaps_detected
                m["gaps_resolved"] = m.get("gaps_resolved", 0) + gaps_resolved
                m["out_of_order_corrected"] = (
                    m.get("out_of_order_corrected", 0) + out_of_order
                )
                if max_gap > m.get("max_gap_size", 0):
                    m["max_gap_size"] = max_gap
                _save_state(state)

        return ordered
        # signed: delta

    def pending_gaps(self) -> List[Dict[str, Any]]:
        """Return info about currently buffered messages waiting for gaps."""
        last = self.last_consumed
        if not self._buffer:
            return []
        gaps = []
        expected = last + 1
        for seq in sorted(self._buffer.keys()):
            if seq > expected:
                gaps.append({
                    "expected": expected,
                    "got": seq,
                    "gap_size": seq - expected,
                    "buffered_age_s": round(
                        time.time() - self._buffer[seq].buffered_at, 2
                    ),
                })
            expected = seq + 1
        return gaps
        # signed: delta

    def flush_buffer(self) -> List[Dict[str, Any]]:
        """Force-release all buffered messages regardless of gaps."""
        ordered = []
        last = self.last_consumed
        for seq in sorted(self._buffer.keys()):
            ordered.append(self._buffer[seq].msg)
            last = seq
        self._buffer.clear()

        if ordered:
            with _lock:
                state = _load_state()
                state["consumers"][self.sender] = last
                m = state["metrics"]
                m["messages_ordered"] = m.get("messages_ordered", 0) + len(ordered)
                m["total_consumed"] = m.get("total_consumed", 0) + len(ordered)
                m["gaps_timed_out"] = m.get("gaps_timed_out", 0) + 1
                _save_state(state)

        return ordered
        # signed: delta

    def reset(self) -> None:
        """Reset consumer state and clear buffer."""
        self._buffer.clear()
        with _lock:
            state = _load_state()
            state["consumers"][self.sender] = 0
            _save_state(state)
        # signed: delta


# ── Integration functions ──────────────────────────────────────────

# Cache publishers per sender to avoid repeated disk reads
_publisher_cache: Dict[str, SequencedPublisher] = {}
_publisher_cache_lock = threading.Lock()


def sequenced_publish(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Publish a message with an auto-incrementing sequence number.

    Drop-in wrapper around ``guarded_publish`` that injects per-sender
    sequence metadata.  Thread-safe.

    Args:
        msg: Message dict with at least ``sender``, ``topic``, ``type``,
             ``content``.

    Returns:
        The augmented message (with ``metadata._seq`` injected).

    Example::

        sequenced_publish({
            "sender": "delta",
            "topic": "orchestrator",
            "type": "result",
            "content": "task done signed:delta",
        })
    # signed: delta
    """
    sender = msg.get("sender", "unknown")
    with _publisher_cache_lock:
        if sender not in _publisher_cache:
            _publisher_cache[sender] = SequencedPublisher(sender)
        pub = _publisher_cache[sender]
    return pub.publish(msg)


# Cache consumers per sender
_consumer_cache: Dict[str, OrderedConsumer] = {}
_consumer_cache_lock = threading.Lock()


def ordered_consume(
    messages: List[Dict[str, Any]],
    sender: str,
    gap_timeout: float = 5.0,
) -> List[Dict[str, Any]]:
    """Consume messages from a sender in guaranteed sequence order.

    Filters ``messages`` for the given sender, reorders by sequence
    number, and detects/resolves gaps.

    Args:
        messages:    Raw bus messages (may include multiple senders).
        sender:      Sender to filter and order for.
        gap_timeout: Seconds to wait for missing messages (default 5.0).

    Returns:
        List of messages in correct sequence order.

    Example::

        raw = requests.get("http://localhost:8420/bus/messages?limit=50").json()
        ordered = ordered_consume(raw, sender="alpha")
    # signed: delta
    """
    with _consumer_cache_lock:
        if sender not in _consumer_cache:
            _consumer_cache[sender] = OrderedConsumer(sender, gap_timeout)
        consumer = _consumer_cache[sender]
    return consumer.consume(messages)


def get_metrics() -> Dict[str, Any]:
    """Return current ordering metrics."""
    with _lock:
        state = _load_state()
        return state.get("metrics", {})
    # signed: delta


def get_all_gaps() -> Dict[str, List[Dict]]:
    """Return pending gaps for all active consumers."""
    result = {}
    with _consumer_cache_lock:
        for sender, consumer in _consumer_cache.items():
            gaps = consumer.pending_gaps()
            if gaps:
                result[sender] = gaps
    return result
    # signed: delta


def get_publisher_state() -> Dict[str, int]:
    """Return current sequence numbers for all publishers."""
    with _lock:
        state = _load_state()
        return dict(state.get("publishers", {}))
    # signed: delta


def get_consumer_state() -> Dict[str, int]:
    """Return last-consumed sequence numbers for all consumers."""
    with _lock:
        state = _load_state()
        return dict(state.get("consumers", {}))
    # signed: delta


def reset_sender(sender: str) -> None:
    """Reset both publisher and consumer state for a sender."""
    with _lock:
        state = _load_state()
        if sender in state["publishers"]:
            state["publishers"][sender] = 1
        if sender in state["consumers"]:
            state["consumers"][sender] = 0
        _save_state(state)

    with _publisher_cache_lock:
        if sender in _publisher_cache:
            _publisher_cache[sender].reset()

    with _consumer_cache_lock:
        if sender in _consumer_cache:
            _consumer_cache[sender].reset()
    # signed: delta


# ── CLI ────────────────────────────────────────────────────────────

def _cli():
    parser = argparse.ArgumentParser(
        description="Skynet Message Ordering — sequence numbers and gap detection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python tools/skynet_message_order.py stats
    python tools/skynet_message_order.py gaps
    python tools/skynet_message_order.py publishers
    python tools/skynet_message_order.py consumers
    python tools/skynet_message_order.py reset delta
    python tools/skynet_message_order.py reset --all
""",
    )
    sub = parser.add_subparsers(dest="command")

    # stats
    sub.add_parser("stats", help="Show ordering metrics")

    # gaps
    sub.add_parser("gaps", help="Show pending gaps across all consumers")

    # publishers
    sub.add_parser("publishers", help="Show publisher sequence numbers")

    # consumers
    sub.add_parser("consumers", help="Show consumer last-consumed sequences")

    # reset
    reset_p = sub.add_parser("reset", help="Reset sequence state for a sender")
    reset_p.add_argument("sender", nargs="?", help="Sender name to reset")
    reset_p.add_argument("--all", action="store_true",
                         help="Reset ALL senders")

    args = parser.parse_args()

    if args.command == "stats":
        metrics = get_metrics()
        print("Message Ordering Metrics")
        print("=" * 40)
        for k, v in sorted(metrics.items()):
            label = k.replace("_", " ").title()
            print(f"  {label:<30} {v}")

    elif args.command == "gaps":
        gaps = get_all_gaps()
        if not gaps:
            print("No pending gaps.")
        else:
            for sender, gap_list in gaps.items():
                print(f"\n  Sender: {sender}")
                for g in gap_list:
                    print(f"    Expected seq {g['expected']}, got {g['got']} "
                          f"(gap={g['gap_size']}, age={g['buffered_age_s']}s)")

    elif args.command == "publishers":
        pubs = get_publisher_state()
        if not pubs:
            print("No publishers registered.")
        else:
            print(f"{'Sender':<25} {'Next Seq':>10}")
            print("-" * 37)
            for sender, seq in sorted(pubs.items()):
                print(f"  {sender:<23} {seq:>10}")

    elif args.command == "consumers":
        cons = get_consumer_state()
        if not cons:
            print("No consumers registered.")
        else:
            print(f"{'Sender':<25} {'Last Consumed':>15}")
            print("-" * 42)
            for sender, seq in sorted(cons.items()):
                print(f"  {sender:<23} {seq:>15}")

    elif args.command == "reset":
        if args.all:
            with _lock:
                state = _load_state()
                for s in list(state["publishers"]):
                    state["publishers"][s] = 1
                for s in list(state["consumers"]):
                    state["consumers"][s] = 0
                _save_state(state)
            print("All senders reset.")
        elif args.sender:
            reset_sender(args.sender)
            print(f"Reset sender '{args.sender}'.")
        else:
            print("Specify a sender name or --all.")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: delta
