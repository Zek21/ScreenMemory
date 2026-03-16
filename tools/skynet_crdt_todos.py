"""Skynet CRDT-Based TODO List (P3.06).

Conflict-free replicated data type (CRDT) for the Skynet TODO list.
Each worker maintains a local replica that can be independently modified
and later merged with any other replica to converge to a consistent state
without coordination or conflict resolution.

CRDT primitives used:
  * **GCounter**     — grow-only counter for completed task counts per worker.
  * **LWWRegister**  — last-writer-wins register (by wall-clock timestamp)
                       for TODO status and metadata fields.
  * **ORSet**        — observed-remove set for the TODO item collection.
                       Add/remove operations commute; concurrent adds win
                       over concurrent removes.

Replicas sync via the Skynet bus every 30 s (configurable).
Merge is idempotent and commutative: merge(A, B) == merge(B, A) and
merge(A, merge(A, B)) == merge(A, B).

State is persisted to ``data/crdt_todos_{worker}.json`` per worker.

CLI
---
    python tools/skynet_crdt_todos.py add    --worker W --title T [--priority P]
    python tools/skynet_crdt_todos.py complete --worker W --id ID
    python tools/skynet_crdt_todos.py sync   --worker W
    python tools/skynet_crdt_todos.py status [--worker W]
    python tools/skynet_crdt_todos.py merge  --worker W --file PATH
    python tools/skynet_crdt_todos.py export --worker W
"""
# signed: beta
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ── Paths & constants ────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SKYNET_PORT = 8420
BUS_URL = f"http://localhost:{SKYNET_PORT}"

WORKER_NAMES = ["alpha", "beta", "gamma", "delta", "orchestrator"]
SYNC_INTERVAL_S = 30
VALID_STATUSES = ("pending", "active", "done", "cancelled")
VALID_PRIORITIES = ("low", "normal", "high", "critical")

logger = logging.getLogger("skynet.crdt_todos")

_lock = threading.Lock()

# ── GCounter ─────────────────────────────────────────────────────

class GCounter:
    """Grow-only counter — each node has its own monotonically increasing slot.

    Used to track completed-task counts per worker.  The global count is
    the sum of all slots.  Merge takes the max of each slot.
    """

    def __init__(self, counts: Optional[Dict[str, int]] = None):
        self._counts: Dict[str, int] = dict(counts or {})
    # signed: beta

    def increment(self, node: str, amount: int = 1) -> None:
        """Increment the counter for *node*."""
        self._counts[node] = self._counts.get(node, 0) + max(0, amount)

    def value(self) -> int:
        """Global counter value (sum of all nodes)."""
        return sum(self._counts.values())

    def get(self, node: str) -> int:
        """Counter value for a single node."""
        return self._counts.get(node, 0)

    def merge(self, other: "GCounter") -> "GCounter":
        """Merge two GCounters — take max per node.  Idempotent."""
        merged = GCounter()
        all_nodes = set(self._counts) | set(other._counts)
        for node in all_nodes:
            merged._counts[node] = max(
                self._counts.get(node, 0),
                other._counts.get(node, 0),
            )
        return merged
    # signed: beta

    def to_dict(self) -> Dict[str, int]:
        return dict(self._counts)

    @classmethod
    def from_dict(cls, d: Dict[str, int]) -> "GCounter":
        return cls(counts=d)


# ── LWWRegister ──────────────────────────────────────────────────

@dataclass
class LWWRegister:
    """Last-Writer-Wins Register — stores a value with a wall-clock timestamp.

    On merge the value with the higher timestamp wins.  Ties are broken
    by lexicographic comparison of the value (deterministic).
    """

    value: Any = None
    timestamp: float = 0.0
    writer: str = ""

    def set(self, value: Any, writer: str = "", ts: Optional[float] = None) -> None:
        """Set a new value (only accepted if ts > current timestamp)."""
        ts = ts or time.time()
        if ts > self.timestamp:
            self.value = value
            self.timestamp = ts
            self.writer = writer
    # signed: beta

    def merge(self, other: "LWWRegister") -> "LWWRegister":
        """Merge two registers — highest timestamp wins."""
        if other.timestamp > self.timestamp:
            return LWWRegister(
                value=other.value, timestamp=other.timestamp,
                writer=other.writer,
            )
        if other.timestamp == self.timestamp:
            # Deterministic tiebreak: lexicographic on str(value)
            if str(other.value) > str(self.value):
                return LWWRegister(
                    value=other.value, timestamp=other.timestamp,
                    writer=other.writer,
                )
        return LWWRegister(
            value=self.value, timestamp=self.timestamp,
            writer=self.writer,
        )
    # signed: beta

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value,
            "timestamp": self.timestamp,
            "writer": self.writer,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LWWRegister":
        return cls(
            value=d.get("value"),
            timestamp=d.get("timestamp", 0.0),
            writer=d.get("writer", ""),
        )


# ── ORSet ────────────────────────────────────────────────────────

class ORSet:
    """Observed-Remove Set — supports concurrent add/remove without conflicts.

    Each element is tagged with a unique token on add.  Remove records which
    tokens have been observed and removed.  An element is in the set iff it
    has at least one token not in the remove set.  Concurrent adds always win
    over concurrent removes (add-wins semantics).
    """

    def __init__(self) -> None:
        # element_id -> set of unique add-tokens
        self._adds: Dict[str, Set[str]] = {}
        # element_id -> set of removed tokens
        self._removes: Dict[str, Set[str]] = {}
    # signed: beta

    def add(self, element_id: str) -> str:
        """Add an element.  Returns the unique token."""
        token = uuid.uuid4().hex[:12]
        self._adds.setdefault(element_id, set()).add(token)
        return token

    def remove(self, element_id: str) -> bool:
        """Remove an element by recording all currently observed tokens."""
        tokens = self._adds.get(element_id, set())
        if not tokens:
            return False
        self._removes.setdefault(element_id, set()).update(tokens)
        return True

    def contains(self, element_id: str) -> bool:
        """Check if an element is in the set (has un-removed tokens)."""
        add_tokens = self._adds.get(element_id, set())
        rem_tokens = self._removes.get(element_id, set())
        return bool(add_tokens - rem_tokens)

    def elements(self) -> Set[str]:
        """Return all elements currently in the set."""
        result: Set[str] = set()
        for eid, add_tokens in self._adds.items():
            rem_tokens = self._removes.get(eid, set())
            if add_tokens - rem_tokens:
                result.add(eid)
        return result

    def merge(self, other: "ORSet") -> "ORSet":
        """Merge two ORSets — union of adds, union of removes."""
        merged = ORSet()
        all_eids = set(self._adds) | set(other._adds)
        for eid in all_eids:
            merged._adds[eid] = (
                self._adds.get(eid, set()) | other._adds.get(eid, set())
            )
            merged._removes[eid] = (
                self._removes.get(eid, set())
                | other._removes.get(eid, set())
            )
        return merged
    # signed: beta

    def to_dict(self) -> Dict[str, Any]:
        return {
            "adds": {k: sorted(v) for k, v in self._adds.items()},
            "removes": {k: sorted(v) for k, v in self._removes.items()},
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ORSet":
        orset = cls()
        orset._adds = {k: set(v) for k, v in d.get("adds", {}).items()}
        orset._removes = {k: set(v) for k, v in d.get("removes", {}).items()}
        return orset


# ── CRDT TODO Item ───────────────────────────────────────────────

class CRDTTodoItem:
    """A single TODO item with LWW registers for mutable fields."""

    def __init__(self, item_id: str, title: str, creator: str = "",
                 priority: str = "normal"):
        self.item_id = item_id
        self.title_reg = LWWRegister(value=title, timestamp=time.time(),
                                      writer=creator)
        self.status_reg = LWWRegister(value="pending", timestamp=time.time(),
                                       writer=creator)
        self.assignee_reg = LWWRegister(value="", timestamp=time.time(),
                                         writer=creator)
        self.priority_reg = LWWRegister(value=priority, timestamp=time.time(),
                                         writer=creator)
        self.created_at: float = time.time()
        self.completed_by: str = ""
    # signed: beta

    @property
    def title(self) -> str:
        return self.title_reg.value or ""

    @property
    def status(self) -> str:
        return self.status_reg.value or "pending"

    @property
    def assignee(self) -> str:
        return self.assignee_reg.value or ""

    @property
    def priority(self) -> str:
        return self.priority_reg.value or "normal"

    def set_status(self, status: str, writer: str = "") -> None:
        if status in VALID_STATUSES:
            self.status_reg.set(status, writer=writer)

    def set_assignee(self, assignee: str, writer: str = "") -> None:
        self.assignee_reg.set(assignee, writer=writer)

    def set_priority(self, priority: str, writer: str = "") -> None:
        if priority in VALID_PRIORITIES:
            self.priority_reg.set(priority, writer=writer)

    def merge(self, other: "CRDTTodoItem") -> "CRDTTodoItem":
        """Merge two versions of the same item — LWW on each field."""
        merged = CRDTTodoItem.__new__(CRDTTodoItem)
        merged.item_id = self.item_id
        merged.title_reg = self.title_reg.merge(other.title_reg)
        merged.status_reg = self.status_reg.merge(other.status_reg)
        merged.assignee_reg = self.assignee_reg.merge(other.assignee_reg)
        merged.priority_reg = self.priority_reg.merge(other.priority_reg)
        merged.created_at = min(self.created_at, other.created_at)
        merged.completed_by = (other.completed_by or self.completed_by)
        return merged
    # signed: beta

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title_reg.to_dict(),
            "status": self.status_reg.to_dict(),
            "assignee": self.assignee_reg.to_dict(),
            "priority": self.priority_reg.to_dict(),
            "created_at": self.created_at,
            "completed_by": self.completed_by,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CRDTTodoItem":
        item = cls.__new__(cls)
        item.item_id = d["item_id"]
        item.title_reg = LWWRegister.from_dict(d.get("title", {}))
        item.status_reg = LWWRegister.from_dict(d.get("status", {}))
        item.assignee_reg = LWWRegister.from_dict(d.get("assignee", {}))
        item.priority_reg = LWWRegister.from_dict(d.get("priority", {}))
        item.created_at = d.get("created_at", 0.0)
        item.completed_by = d.get("completed_by", "")
        return item


# ── CRDT TODO List ───────────────────────────────────────────────

class CRDTTodoList:
    """Conflict-free replicated TODO list.

    Combines ORSet (item membership), LWWRegister (per-field last-writer-wins),
    and GCounter (completed counts) into a single mergeable state.

    Each worker holds a local replica.  ``merge(remote)`` produces a new
    replica that is the union of both, with LWW semantics on mutable fields.
    """

    def __init__(self, owner: str = ""):
        self.owner = owner
        self.items_set = ORSet()          # which item_ids exist
        self.items: Dict[str, CRDTTodoItem] = {}  # item_id -> item
        self.completed_counter = GCounter()       # per-worker completed count
        self.version: int = 0
        self.last_sync: float = 0.0
    # signed: beta

    # ── Mutations ────────────────────────────────────────────────

    def add_item(self, title: str, priority: str = "normal",
                 assignee: str = "", item_id: Optional[str] = None) -> str:
        """Add a new TODO item.  Returns the item_id."""
        item_id = item_id or f"crdt_{uuid.uuid4().hex[:10]}"
        self.items_set.add(item_id)
        item = CRDTTodoItem(item_id, title, creator=self.owner,
                            priority=priority)
        if assignee:
            item.set_assignee(assignee, writer=self.owner)
        self.items[item_id] = item
        self.version += 1
        return item_id
    # signed: beta

    def complete_item(self, item_id: str, worker: Optional[str] = None) -> bool:
        """Mark an item as done and increment the worker's completed counter."""
        item = self.items.get(item_id)
        if not item:
            return False
        worker = worker or self.owner
        item.set_status("done", writer=worker)
        item.completed_by = worker
        self.completed_counter.increment(worker)
        self.version += 1
        return True
    # signed: beta

    def cancel_item(self, item_id: str, worker: Optional[str] = None) -> bool:
        """Cancel a TODO item."""
        item = self.items.get(item_id)
        if not item:
            return False
        item.set_status("cancelled", writer=worker or self.owner)
        self.version += 1
        return True

    def activate_item(self, item_id: str, worker: Optional[str] = None) -> bool:
        """Set item status to active."""
        item = self.items.get(item_id)
        if not item:
            return False
        item.set_status("active", writer=worker or self.owner)
        self.version += 1
        return True

    def assign_item(self, item_id: str, assignee: str,
                    writer: Optional[str] = None) -> bool:
        """Assign an item to a worker."""
        item = self.items.get(item_id)
        if not item:
            return False
        item.set_assignee(assignee, writer=writer or self.owner)
        self.version += 1
        return True

    def remove_item(self, item_id: str) -> bool:
        """Remove an item from the set (observed-remove)."""
        if not self.items_set.contains(item_id):
            return False
        self.items_set.remove(item_id)
        self.version += 1
        return True

    # ── Merge ────────────────────────────────────────────────────

    def merge(self, remote: "CRDTTodoList") -> "CRDTTodoList":
        """Merge this replica with a remote replica.

        Returns a new CRDTTodoList that is the convergence of both.
        This operation is idempotent and commutative.
        """
        merged = CRDTTodoList(owner=self.owner)

        # 1. Merge ORSet (item membership)
        merged.items_set = self.items_set.merge(remote.items_set)

        # 2. Merge GCounter (completed counts)
        merged.completed_counter = self.completed_counter.merge(
            remote.completed_counter
        )

        # 3. Merge item data (LWW per field)
        all_ids = set(self.items) | set(remote.items)
        for item_id in all_ids:
            local_item = self.items.get(item_id)
            remote_item = remote.items.get(item_id)
            if local_item and remote_item:
                merged.items[item_id] = local_item.merge(remote_item)
            elif local_item:
                merged.items[item_id] = copy.deepcopy(local_item)
            else:
                merged.items[item_id] = copy.deepcopy(remote_item)

        merged.version = max(self.version, remote.version) + 1
        merged.last_sync = time.time()
        return merged
    # signed: beta

    # ── Queries ──────────────────────────────────────────────────

    def active_items(self) -> List[CRDTTodoItem]:
        """Items currently in the ORSet with non-done/cancelled status."""
        live_ids = self.items_set.elements()
        result = []
        for item_id in live_ids:
            item = self.items.get(item_id)
            if item and item.status in ("pending", "active"):
                result.append(item)
        return sorted(result, key=lambda i: (
            {"critical": 0, "high": 1, "normal": 2, "low": 3}.get(i.priority, 2),
            i.created_at,
        ))

    def all_items(self) -> List[CRDTTodoItem]:
        """All items in the ORSet (including done/cancelled)."""
        live_ids = self.items_set.elements()
        return [self.items[iid] for iid in live_ids if iid in self.items]

    def completed_count(self, worker: Optional[str] = None) -> int:
        """Completed count — total or per-worker."""
        if worker:
            return self.completed_counter.get(worker)
        return self.completed_counter.value()

    def pending_count(self, worker: Optional[str] = None) -> int:
        """Count of pending+active items, optionally filtered by assignee."""
        items = self.active_items()
        if worker:
            items = [i for i in items if i.assignee == worker]
        return len(items)

    def status_summary(self) -> Dict[str, Any]:
        """Human-readable status summary."""
        live = self.items_set.elements()
        all_items = [self.items[i] for i in live if i in self.items]
        by_status: Dict[str, int] = {}
        by_worker: Dict[str, int] = {}
        for item in all_items:
            by_status[item.status] = by_status.get(item.status, 0) + 1
            if item.assignee:
                by_worker[item.assignee] = by_worker.get(item.assignee, 0) + 1

        return {
            "owner": self.owner,
            "version": self.version,
            "total_items": len(all_items),
            "by_status": by_status,
            "by_worker": by_worker,
            "completed_counter": self.completed_counter.to_dict(),
            "total_completed": self.completed_counter.value(),
            "last_sync": self.last_sync,
            "orset_size": len(live),
        }
    # signed: beta

    # ── Persistence ──────────────────────────────────────────────

    def save(self, path: Optional[Path] = None) -> Path:
        """Save replica to disk (atomic write)."""
        path = path or (DATA_DIR / f"crdt_todos_{self.owner}.json")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        state = {
            "owner": self.owner,
            "version": self.version,
            "last_sync": self.last_sync,
            "items_set": self.items_set.to_dict(),
            "items": {k: v.to_dict() for k, v in self.items.items()},
            "completed_counter": self.completed_counter.to_dict(),
            "saved_at": time.time(),
        }
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        tmp.replace(path)
        return path

    @classmethod
    def load(cls, owner: str, path: Optional[Path] = None) -> "CRDTTodoList":
        """Load replica from disk."""
        path = path or (DATA_DIR / f"crdt_todos_{owner}.json")
        tdl = cls(owner=owner)
        if not path.exists():
            return tdl
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            return tdl

        tdl.owner = state.get("owner", owner)
        tdl.version = state.get("version", 0)
        tdl.last_sync = state.get("last_sync", 0.0)
        tdl.items_set = ORSet.from_dict(state.get("items_set", {}))
        tdl.completed_counter = GCounter.from_dict(
            state.get("completed_counter", {})
        )
        for iid, idata in state.get("items", {}).items():
            tdl.items[iid] = CRDTTodoItem.from_dict(idata)
        return tdl
    # signed: beta

    # ── Bus Sync ─────────────────────────────────────────────────

    def sync_to_bus(self) -> bool:
        """Publish this replica's state to the bus for other workers to merge."""
        try:
            from tools.skynet_spam_guard import guarded_publish
        except ImportError:
            logger.warning("SpamGuard not available, skipping bus sync")
            return False

        state_json = json.dumps({
            "owner": self.owner,
            "version": self.version,
            "items_set": self.items_set.to_dict(),
            "items": {k: v.to_dict() for k, v in self.items.items()},
            "completed_counter": self.completed_counter.to_dict(),
        })
        result = guarded_publish({
            "sender": self.owner,
            "topic": "todos",
            "type": "crdt_sync",
            "content": state_json[:4000],  # bus message size limit
        })
        if result.get("published"):
            self.last_sync = time.time()
            self.save()
        return result.get("published", False)
    # signed: beta

    @classmethod
    def merge_from_bus_message(cls, local: "CRDTTodoList",
                                msg_content: str) -> "CRDTTodoList":
        """Merge a bus message payload into the local replica."""
        try:
            remote_data = json.loads(msg_content)
        except json.JSONDecodeError:
            logger.warning("Invalid CRDT sync message")
            return local

        remote = cls(owner=remote_data.get("owner", "unknown"))
        remote.version = remote_data.get("version", 0)
        remote.items_set = ORSet.from_dict(remote_data.get("items_set", {}))
        remote.completed_counter = GCounter.from_dict(
            remote_data.get("completed_counter", {})
        )
        for iid, idata in remote_data.get("items", {}).items():
            remote.items[iid] = CRDTTodoItem.from_dict(idata)

        return local.merge(remote)


# ── Convenience Functions ────────────────────────────────────────

_replicas: Dict[str, CRDTTodoList] = {}


def get_replica(worker: str) -> CRDTTodoList:
    """Get or load the local replica for a worker."""
    with _lock:
        if worker not in _replicas:
            _replicas[worker] = CRDTTodoList.load(worker)
        return _replicas[worker]


def crdt_add(worker: str, title: str, priority: str = "normal",
             assignee: str = "") -> str:
    """Add a TODO item to a worker's replica."""
    replica = get_replica(worker)
    item_id = replica.add_item(title, priority=priority, assignee=assignee)
    replica.save()
    return item_id
# signed: beta


def crdt_complete(worker: str, item_id: str) -> bool:
    """Complete a TODO item in a worker's replica."""
    replica = get_replica(worker)
    ok = replica.complete_item(item_id, worker=worker)
    if ok:
        replica.save()
    return ok


def crdt_sync(worker: str) -> bool:
    """Sync a worker's replica to the bus."""
    replica = get_replica(worker)
    return replica.sync_to_bus()


def crdt_status(worker: Optional[str] = None) -> Dict[str, Any]:
    """Get status summary for a worker or all workers."""
    if worker:
        replica = get_replica(worker)
        return replica.status_summary()

    summaries = {}
    for w in WORKER_NAMES:
        path = DATA_DIR / f"crdt_todos_{w}.json"
        if path.exists():
            r = CRDTTodoList.load(w)
            summaries[w] = r.status_summary()
    return summaries
# signed: beta


def crdt_merge_file(worker: str, file_path: str) -> Dict[str, Any]:
    """Merge a remote state file into a worker's local replica."""
    local = get_replica(worker)
    remote = CRDTTodoList.load("_remote", path=Path(file_path))
    merged = local.merge(remote)
    merged.save()
    _replicas[worker] = merged
    return merged.status_summary()


def crdt_export(worker: str) -> Dict[str, Any]:
    """Export a worker's replica as a dict."""
    replica = get_replica(worker)
    return {
        "owner": replica.owner,
        "version": replica.version,
        "items_set": replica.items_set.to_dict(),
        "items": {k: v.to_dict() for k, v in replica.items.items()},
        "completed_counter": replica.completed_counter.to_dict(),
        "last_sync": replica.last_sync,
    }


# ── CLI ──────────────────────────────────────────────────────────

def _cli() -> None:
    """Command-line interface."""
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    parser = argparse.ArgumentParser(
        description="Skynet CRDT TODO List (conflict-free replicated)")
    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add", help="Add a TODO item")
    p_add.add_argument("--worker", required=True, help="Worker name")
    p_add.add_argument("--title", required=True, help="TODO title")
    p_add.add_argument("--priority", default="normal",
                       choices=list(VALID_PRIORITIES))
    p_add.add_argument("--assignee", default="", help="Assign to worker")

    # complete
    p_comp = sub.add_parser("complete", help="Complete a TODO item")
    p_comp.add_argument("--worker", required=True)
    p_comp.add_argument("--id", required=True, help="Item ID")

    # sync
    p_sync = sub.add_parser("sync", help="Sync replica to bus")
    p_sync.add_argument("--worker", required=True)

    # status
    p_status = sub.add_parser("status", help="Show TODO status")
    p_status.add_argument("--worker", default=None)
    p_status.add_argument("--json", action="store_true",
                          dest="json_output")

    # merge
    p_merge = sub.add_parser("merge", help="Merge remote state file")
    p_merge.add_argument("--worker", required=True)
    p_merge.add_argument("--file", required=True, help="Remote state file")

    # export
    p_export = sub.add_parser("export", help="Export replica as JSON")
    p_export.add_argument("--worker", required=True)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    if args.command == "add":
        item_id = crdt_add(args.worker, args.title,
                           priority=args.priority, assignee=args.assignee)
        print(f"Added: {item_id}")

    elif args.command == "complete":
        ok = crdt_complete(args.worker, args.id)
        print(f"{'Completed' if ok else 'NOT FOUND'}: {args.id}")

    elif args.command == "sync":
        ok = crdt_sync(args.worker)
        print(f"{'Synced' if ok else 'Sync failed'}")

    elif args.command == "status":
        summary = crdt_status(args.worker)
        if getattr(args, "json_output", False):
            print(json.dumps(summary, indent=2))
        else:
            if args.worker:
                _print_summary(summary)
            else:
                for w, s in summary.items():
                    print(f"\n=== {w.upper()} ===")
                    _print_summary(s)

    elif args.command == "merge":
        result = crdt_merge_file(args.worker, args.file)
        print("Merged. New state:")
        _print_summary(result)

    elif args.command == "export":
        data = crdt_export(args.worker)
        print(json.dumps(data, indent=2))


def _print_summary(s: Dict[str, Any]) -> None:
    """Print a status summary."""
    print(f"  Owner: {s.get('owner', '?')}")
    print(f"  Version: {s.get('version', 0)}")
    print(f"  Total items: {s.get('total_items', 0)}")
    print(f"  By status: {s.get('by_status', {})}")
    print(f"  By worker: {s.get('by_worker', {})}")
    print(f"  Completed total: {s.get('total_completed', 0)}")
    cc = s.get("completed_counter", {})
    if cc:
        print(f"  Completed per worker: {cc}")
    ls = s.get("last_sync", 0)
    if ls > 0:
        import datetime as _dt
        ts = _dt.datetime.fromtimestamp(ls).strftime("%H:%M:%S")
        print(f"  Last sync: {ts}")


if __name__ == "__main__":
    _cli()
# signed: beta
