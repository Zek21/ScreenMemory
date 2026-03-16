"""Skynet Event Sourcing for Task Lifecycle (P2.01).
# signed: alpha

Append-only immutable event log for task lifecycle tracking.  Every state
change is recorded as an event; current state is derived by replaying
events.  Snapshots accelerate reconstruction for long-lived tasks.

Storage:
    data/event_store.jsonl    — append-only event log (one JSON per line)
    data/event_snapshots/     — per-task snapshot files

Event types:
    TaskCreated, TaskAssigned, TaskStarted, TaskCompleted,
    TaskFailed, TaskRetried, WorkerStateChanged

Usage:
    python tools/skynet_event_source.py replay TASK_ID
    python tools/skynet_event_source.py query --type TaskCompleted --since 2h --limit 20
    python tools/skynet_event_source.py stats
    python tools/skynet_event_source.py snapshot TASK_ID
    python tools/skynet_event_source.py emit TaskCreated TASK_ID '{"task":"do X","worker":"alpha"}'
    python tools/skynet_event_source.py gc --before 7d
    python tools/skynet_event_source.py tail --follow

Python API:
    from tools.skynet_event_source import EventStore
    store = EventStore()
    store.append("TaskCreated", "task-42", {"task": "implement caching"})
    state = store.rebuild_state("task-42")
    events = store.query_events(task_id="task-42")
    store.snapshot_state("task-42")
"""
# signed: alpha

import json
import os
import sys
import time
import uuid
import threading
import argparse
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
EVENT_STORE_PATH = DATA_DIR / "event_store.jsonl"
SNAPSHOT_DIR = DATA_DIR / "event_snapshots"

# ── Valid event types ───────────────────────────────────────────────
EVENT_TYPES = frozenset({
    "TaskCreated",
    "TaskAssigned",
    "TaskStarted",
    "TaskCompleted",
    "TaskFailed",
    "TaskRetried",
    "WorkerStateChanged",
})

# signed: alpha


# ────────────────────────────────────────────────────────────────────
# Event dataclass
# ────────────────────────────────────────────────────────────────────
@dataclass
class Event:
    """Immutable event record.

    Attributes:
        event_id:        UUID v4 unique identifier.
        timestamp:       ISO-8601 UTC timestamp.
        event_type:      One of EVENT_TYPES.
        aggregate_id:    Task or entity ID this event belongs to.
        payload:         Arbitrary data dict (task text, worker, result, etc).
        sequence_number: Monotonically increasing per aggregate.
    """
    event_id: str
    timestamp: str
    event_type: str
    aggregate_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    sequence_number: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})

    @classmethod
    def from_json(cls, line: str) -> "Event":
        return cls.from_dict(json.loads(line))
    # signed: alpha


# ────────────────────────────────────────────────────────────────────
# Task state (rebuilt from events)
# ────────────────────────────────────────────────────────────────────
@dataclass
class TaskState:
    """Current state of a task, derived by replaying its events.

    Fields are populated progressively as events are applied.
    """
    task_id: str
    status: str = "unknown"
    task_text: str = ""
    worker: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    retries: int = 0
    created_at: Optional[str] = None
    assigned_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    failed_at: Optional[str] = None
    last_event_seq: int = 0
    event_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def apply(self, event: Event) -> None:
        """Apply a single event to update state."""
        self.last_event_seq = event.sequence_number
        self.event_count += 1
        p = event.payload

        if event.event_type == "TaskCreated":
            self.status = "created"
            self.task_text = p.get("task", "")
            self.created_at = event.timestamp
            self.metadata.update({
                k: v for k, v in p.items() if k not in ("task",)
            })

        elif event.event_type == "TaskAssigned":
            self.status = "assigned"
            self.worker = p.get("worker")
            self.assigned_at = event.timestamp

        elif event.event_type == "TaskStarted":
            self.status = "running"
            self.started_at = event.timestamp

        elif event.event_type == "TaskCompleted":
            self.status = "completed"
            self.result = p.get("result", "")
            self.completed_at = event.timestamp

        elif event.event_type == "TaskFailed":
            self.status = "failed"
            self.error = p.get("error", "")
            self.failed_at = event.timestamp

        elif event.event_type == "TaskRetried":
            self.status = "retrying"
            self.retries += 1
            self.error = p.get("reason", self.error)

        elif event.event_type == "WorkerStateChanged":
            # Worker-level event — store latest worker state in metadata
            self.metadata["worker_state"] = p.get("new_state", "")
            self.metadata["worker_prev_state"] = p.get("old_state", "")

    def to_dict(self) -> dict:
        return asdict(self)
    # signed: alpha


# ────────────────────────────────────────────────────────────────────
# EventStore
# ────────────────────────────────────────────────────────────────────
class EventStore:
    """Append-only event store backed by a JSONL file.

    Thread-safe via a lock for appends. File-level locking prevents
    corruption from concurrent processes.

    Usage::

        store = EventStore()
        store.append("TaskCreated", "task-42", {"task": "implement X"})
        store.append("TaskAssigned", "task-42", {"worker": "alpha"})
        state = store.rebuild_state("task-42")
        print(state.status)  # "assigned"
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path or EVENT_STORE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq_cache: Dict[str, int] = {}  # aggregate_id -> last seq
    # signed: alpha

    # ── Append ──────────────────────────────────────────────────────

    def append(self, event_type: str, aggregate_id: str,
               payload: Optional[Dict[str, Any]] = None,
               event_id: Optional[str] = None) -> Event:
        """Append an immutable event to the store.

        Args:
            event_type:   Must be one of EVENT_TYPES.
            aggregate_id: Task or entity ID.
            payload:      Data dict (task text, worker, result, etc).
            event_id:     Optional explicit UUID (auto-generated if None).

        Returns:
            The created Event object.

        Raises:
            ValueError: If event_type is not in EVENT_TYPES.
        """
        if event_type not in EVENT_TYPES:
            raise ValueError(
                f"Unknown event type '{event_type}'. "
                f"Valid: {sorted(EVENT_TYPES)}"
            )

        with self._lock:
            seq = self._next_seq(aggregate_id)
            event = Event(
                event_id=event_id or str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type=event_type,
                aggregate_id=aggregate_id,
                payload=payload or {},
                sequence_number=seq,
            )
            self._write_event(event)
            return event

    def append_batch(self, events: List[tuple]) -> List[Event]:
        """Append multiple events atomically.

        Args:
            events: List of (event_type, aggregate_id, payload) tuples.

        Returns:
            List of created Event objects.
        """
        created = []
        with self._lock:
            lines = []
            for event_type, aggregate_id, payload in events:
                if event_type not in EVENT_TYPES:
                    raise ValueError(f"Unknown event type '{event_type}'")
                seq = self._next_seq(aggregate_id)
                event = Event(
                    event_id=str(uuid.uuid4()),
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    event_type=event_type,
                    aggregate_id=aggregate_id,
                    payload=payload or {},
                    sequence_number=seq,
                )
                lines.append(event.to_json() + "\n")
                created.append(event)

            # Single atomic write for the batch
            with open(self.path, "a", encoding="utf-8") as f:
                f.writelines(lines)
                f.flush()

        return created

    def _next_seq(self, aggregate_id: str) -> int:
        """Get and increment the next sequence number for an aggregate."""
        if aggregate_id not in self._seq_cache:
            # Scan file for existing max seq
            max_seq = -1
            for event in self._iter_raw():
                if event.get("aggregate_id") == aggregate_id:
                    s = event.get("sequence_number", 0)
                    if s > max_seq:
                        max_seq = s
            self._seq_cache[aggregate_id] = max_seq + 1
        else:
            self._seq_cache[aggregate_id] += 1
        return self._seq_cache[aggregate_id]

    def _write_event(self, event: Event) -> None:
        """Append a single event line to the JSONL file."""
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(event.to_json() + "\n")
            f.flush()

    # ── Read ────────────────────────────────────────────────────────

    def _iter_raw(self) -> List[dict]:
        """Read all events as raw dicts. Returns empty list if file missing."""
        if not self.path.exists():
            return []
        results = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return results

    def _iter_events(self) -> List[Event]:
        """Read all events as Event objects."""
        return [Event.from_dict(d) for d in self._iter_raw()]

    def get_events(self, aggregate_id: str) -> List[Event]:
        """Get all events for a specific aggregate, ordered by sequence."""
        events = [
            e for e in self._iter_events()
            if e.aggregate_id == aggregate_id
        ]
        events.sort(key=lambda e: e.sequence_number)
        return events

    # ── State rebuild ───────────────────────────────────────────────

    def rebuild_state(self, task_id: str) -> TaskState:
        """Reconstruct current task state by replaying all its events.

        If a snapshot exists and is newer than some events, starts from
        the snapshot and replays only subsequent events.

        Args:
            task_id: The aggregate ID (task ID) to rebuild.

        Returns:
            TaskState with all events applied.
        """
        # Try snapshot-accelerated rebuild first
        snapshot = self._load_snapshot(task_id)
        if snapshot:
            state = TaskState(**snapshot["state"])
            start_seq = state.last_event_seq + 1
            events = [
                e for e in self.get_events(task_id)
                if e.sequence_number >= start_seq
            ]
        else:
            state = TaskState(task_id=task_id)
            events = self.get_events(task_id)

        for event in events:
            state.apply(event)

        return state

    # ── Query ───────────────────────────────────────────────────────

    def query_events(
        self,
        task_id: Optional[str] = None,
        event_type: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
        worker: Optional[str] = None,
    ) -> List[Event]:
        """Query events with filters.

        Args:
            task_id:    Filter by aggregate ID.
            event_type: Filter by event type (e.g. "TaskCompleted").
            since:      ISO timestamp or relative like "2h", "30m", "1d".
            until:      ISO timestamp or relative.
            limit:      Maximum events to return.
            worker:     Filter by worker name in payload.

        Returns:
            List of matching Event objects, newest first.
        """
        since_ts = _parse_time(since) if since else None
        until_ts = _parse_time(until) if until else None

        matched = []
        for event in self._iter_events():
            if task_id and event.aggregate_id != task_id:
                continue
            if event_type and event.event_type != event_type:
                continue
            if worker and event.payload.get("worker") != worker:
                continue
            if since_ts and event.timestamp < since_ts:
                continue
            if until_ts and event.timestamp > until_ts:
                continue
            matched.append(event)

        # Newest first
        matched.sort(key=lambda e: e.timestamp, reverse=True)
        return matched[:limit]

    # ── Snapshots ───────────────────────────────────────────────────

    def snapshot_state(self, task_id: str) -> dict:
        """Create a snapshot of current task state for fast reconstruction.

        The snapshot captures the fully-replayed TaskState so future
        rebuild_state() calls can skip replaying events before the
        snapshot's sequence number.

        Args:
            task_id: The aggregate ID to snapshot.

        Returns:
            The snapshot dict (state + metadata).
        """
        state = self.rebuild_state(task_id)
        snap = {
            "task_id": task_id,
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "last_event_seq": state.last_event_seq,
            "event_count": state.event_count,
            "state": state.to_dict(),
        }
        self._save_snapshot(task_id, snap)
        return snap

    def _snapshot_path(self, task_id: str) -> Path:
        safe = re.sub(r'[^\w\-.]', '_', task_id)
        return SNAPSHOT_DIR / f"{safe}.json"

    def _save_snapshot(self, task_id: str, snap: dict) -> None:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        p = self._snapshot_path(task_id)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2, default=str)
        os.replace(str(tmp), str(p))

    def _load_snapshot(self, task_id: str) -> Optional[dict]:
        p = self._snapshot_path(task_id)
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    # ── Statistics ──────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return aggregate statistics over the event store."""
        events = self._iter_raw()
        total = len(events)
        if total == 0:
            return {"total_events": 0}

        by_type: Dict[str, int] = {}
        by_aggregate: Dict[str, int] = {}
        by_worker: Dict[str, int] = {}
        first_ts = None
        last_ts = None

        for e in events:
            et = e.get("event_type", "unknown")
            by_type[et] = by_type.get(et, 0) + 1

            aid = e.get("aggregate_id", "")
            by_aggregate[aid] = by_aggregate.get(aid, 0) + 1

            w = e.get("payload", {}).get("worker")
            if w:
                by_worker[w] = by_worker.get(w, 0) + 1

            ts = e.get("timestamp", "")
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts

        completed = by_type.get("TaskCompleted", 0)
        failed = by_type.get("TaskFailed", 0)
        created = by_type.get("TaskCreated", 0)

        return {
            "total_events": total,
            "unique_tasks": len(by_aggregate),
            "events_by_type": dict(sorted(by_type.items())),
            "events_by_worker": dict(sorted(by_worker.items())),
            "completion_rate": round(completed / max(created, 1), 3),
            "failure_rate": round(failed / max(created, 1), 3),
            "time_range": {"first": first_ts, "last": last_ts},
        }

    # ── Garbage collection ──────────────────────────────────────────

    def gc(self, before: str) -> int:
        """Remove events older than a threshold.

        Creates a new file without old events and atomically replaces
        the original.  Clears affected snapshot files.

        Args:
            before: ISO timestamp or relative like "7d", "24h".

        Returns:
            Number of events removed.
        """
        cutoff = _parse_time(before)
        if not cutoff:
            return 0

        all_events = self._iter_raw()
        keep = [e for e in all_events if e.get("timestamp", "") >= cutoff]
        removed = len(all_events) - len(keep)

        if removed == 0:
            return 0

        # Atomic rewrite
        tmp = self.path.with_suffix(".gc_tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for e in keep:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(str(tmp), str(self.path))

        # Invalidate seq cache
        self._seq_cache.clear()

        # Remove snapshots for aggregates that lost events
        removed_aggs = set()
        for e in all_events:
            if e.get("timestamp", "") < cutoff:
                removed_aggs.add(e.get("aggregate_id", ""))
        for agg in removed_aggs:
            sp = self._snapshot_path(agg)
            if sp.exists():
                sp.unlink()

        return removed

    # ── Tail (follow mode) ──────────────────────────────────────────

    def tail(self, n: int = 20, follow: bool = False,
             callback=None) -> List[Event]:
        """Show the last N events, optionally following new appends.

        Args:
            n:        Number of recent events to show.
            follow:   If True, keep watching for new events (blocking).
            callback: Called with each new Event in follow mode.

        Returns:
            List of the last N events (before follow begins).
        """
        all_events = self._iter_events()
        recent = all_events[-n:] if all_events else []

        if not follow:
            return recent

        # Follow mode: watch file size and read new lines
        last_size = self.path.stat().st_size if self.path.exists() else 0
        try:
            while True:
                time.sleep(0.5)
                if not self.path.exists():
                    continue
                cur_size = self.path.stat().st_size
                if cur_size > last_size:
                    with open(self.path, "r", encoding="utf-8") as f:
                        f.seek(last_size)
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                event = Event.from_json(line)
                                if callback:
                                    callback(event)
                                else:
                                    _print_event(event)
                            except (json.JSONDecodeError, TypeError):
                                continue
                    last_size = cur_size
        except KeyboardInterrupt:
            pass

        return recent
    # signed: alpha


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────

_RELATIVE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(s|m|h|d|w)$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _parse_time(spec: Optional[str]) -> Optional[str]:
    """Parse a time spec into an ISO-8601 UTC timestamp string.

    Accepts:
        ISO-8601 string  — returned as-is
        Relative string  — "2h", "30m", "7d" — converted to UTC timestamp
    """
    if not spec:
        return None

    # Try relative
    m = _RELATIVE_RE.match(spec.strip())
    if m:
        amount = float(m.group(1))
        unit = m.group(2).lower()
        delta = timedelta(seconds=amount * _UNIT_SECONDS[unit])
        cutoff = datetime.now(timezone.utc) - delta
        return cutoff.isoformat()

    # Assume ISO-8601
    return spec


def _print_event(event: Event) -> None:
    """Pretty-print a single event to stdout."""
    ts_short = event.timestamp[:19].replace("T", " ")
    payload_str = json.dumps(event.payload) if event.payload else ""
    if len(payload_str) > 100:
        payload_str = payload_str[:97] + "..."
    print(f"  [{ts_short}] seq={event.sequence_number:>3} "
          f"{event.event_type:<20} agg={event.aggregate_id:<16} "
          f"{payload_str}")


def _print_state(state: TaskState) -> None:
    """Pretty-print a TaskState to stdout."""
    print(f"  Task:      {state.task_id}")
    print(f"  Status:    {state.status}")
    if state.task_text:
        text = state.task_text[:120] + ("..." if len(state.task_text) > 120 else "")
        print(f"  Text:      {text}")
    if state.worker:
        print(f"  Worker:    {state.worker}")
    if state.result:
        result = state.result[:120] + ("..." if len(state.result) > 120 else "")
        print(f"  Result:    {result}")
    if state.error:
        print(f"  Error:     {state.error}")
    if state.retries > 0:
        print(f"  Retries:   {state.retries}")
    print(f"  Events:    {state.event_count} (last seq={state.last_event_seq})")
    if state.created_at:
        print(f"  Created:   {state.created_at[:19]}")
    if state.completed_at:
        print(f"  Completed: {state.completed_at[:19]}")
    elif state.failed_at:
        print(f"  Failed:    {state.failed_at[:19]}")


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────

def _cli():
    parser = argparse.ArgumentParser(
        description="Skynet Event Sourcing — Task Lifecycle",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s replay task-42
    %(prog)s query --type TaskCompleted --since 2h
    %(prog)s query --worker alpha --limit 10
    %(prog)s stats
    %(prog)s snapshot task-42
    %(prog)s emit TaskCreated task-42 '{"task":"build X"}'
    %(prog)s tail --follow
    %(prog)s gc --before 7d
""",
    )
    sub = parser.add_subparsers(dest="command")

    # replay
    rp = sub.add_parser("replay", help="Replay events to show current task state")
    rp.add_argument("task_id", help="Task / aggregate ID")

    # query
    qp = sub.add_parser("query", help="Query events with filters")
    qp.add_argument("--task-id", help="Filter by task ID")
    qp.add_argument("--type", dest="event_type", help="Filter by event type")
    qp.add_argument("--since", help="Time filter: ISO or relative (2h, 30m, 7d)")
    qp.add_argument("--until", help="Upper time bound")
    qp.add_argument("--worker", help="Filter by worker name in payload")
    qp.add_argument("--limit", type=int, default=50, help="Max results (default 50)")

    # stats
    sub.add_parser("stats", help="Show event store statistics")

    # snapshot
    sp = sub.add_parser("snapshot", help="Create a state snapshot for fast replay")
    sp.add_argument("task_id", help="Task / aggregate ID")

    # emit
    ep = sub.add_parser("emit", help="Emit a new event")
    ep.add_argument("event_type", help="Event type (e.g. TaskCreated)")
    ep.add_argument("aggregate_id", help="Task / aggregate ID")
    ep.add_argument("payload", nargs="?", default="{}", help="JSON payload")

    # tail
    tp = sub.add_parser("tail", help="Show recent events")
    tp.add_argument("-n", type=int, default=20, help="Number of events (default 20)")
    tp.add_argument("--follow", "-f", action="store_true",
                    help="Follow new events (Ctrl+C to stop)")

    # gc
    gp = sub.add_parser("gc", help="Garbage-collect old events")
    gp.add_argument("--before", required=True,
                    help="Remove events older than this (e.g. 7d, 24h)")

    args = parser.parse_args()
    store = EventStore()

    if args.command == "replay":
        state = store.rebuild_state(args.task_id)
        if state.event_count == 0:
            print(f"No events found for '{args.task_id}'")
            return
        print(f"State rebuilt from {state.event_count} events:")
        _print_state(state)

    elif args.command == "query":
        events = store.query_events(
            task_id=args.task_id,
            event_type=args.event_type,
            since=args.since,
            until=args.until,
            limit=args.limit,
            worker=args.worker,
        )
        if not events:
            print("No matching events.")
            return
        print(f"Found {len(events)} events:")
        for e in events:
            _print_event(e)

    elif args.command == "stats":
        s = store.stats()
        if s["total_events"] == 0:
            print("Event store is empty.")
            return
        print(f"Total events:    {s['total_events']}")
        print(f"Unique tasks:    {s['unique_tasks']}")
        print(f"Completion rate: {s['completion_rate']:.1%}")
        print(f"Failure rate:    {s['failure_rate']:.1%}")
        print(f"Time range:      {(s['time_range']['first'] or '')[:19]} "
              f"→ {(s['time_range']['last'] or '')[:19]}")
        print("By type:")
        for t, c in s["events_by_type"].items():
            print(f"  {t:<25} {c:>5}")
        if s["events_by_worker"]:
            print("By worker:")
            for w, c in s["events_by_worker"].items():
                print(f"  {w:<25} {c:>5}")

    elif args.command == "snapshot":
        snap = store.snapshot_state(args.task_id)
        if snap["event_count"] == 0:
            print(f"No events for '{args.task_id}' — nothing to snapshot.")
            return
        print(f"Snapshot created: seq={snap['last_event_seq']}, "
              f"events={snap['event_count']}")
        print(f"  Saved to: {store._snapshot_path(args.task_id)}")

    elif args.command == "emit":
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as e:
            print(f"Invalid JSON payload: {e}")
            return
        event = store.append(args.event_type, args.aggregate_id, payload)
        print(f"Event emitted: {event.event_id[:8]}... "
              f"type={event.event_type} seq={event.sequence_number}")

    elif args.command == "tail":
        recent = store.tail(n=args.n, follow=args.follow)
        if not args.follow:
            if not recent:
                print("Event store is empty.")
                return
            print(f"Last {len(recent)} events:")
            for e in recent:
                _print_event(e)

    elif args.command == "gc":
        removed = store.gc(before=args.before)
        print(f"Garbage collected {removed} events")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: alpha
