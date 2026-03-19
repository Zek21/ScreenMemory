#!/usr/bin/env python3
"""External Worker Quarantine System.

All results from non-core workers must pass through quarantine before
being treated as trusted.  Core workers (alpha, beta, gamma, delta) can
approve or reject quarantined entries.  Stale entries auto-expire.

CLI usage:
    python tools/skynet_external_quarantine.py submit  --worker W --task T --result R
    python tools/skynet_external_quarantine.py pending
    python tools/skynet_external_quarantine.py approve --id ID --validator V [--notes N]
    python tools/skynet_external_quarantine.py reject  --id ID --validator V --reason R
    python tools/skynet_external_quarantine.py stats
    python tools/skynet_external_quarantine.py expire
    python tools/skynet_external_quarantine.py get     --id ID
"""
# signed: alpha

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

CORE_WORKERS = frozenset({"alpha", "beta", "gamma", "delta"})
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
QUARANTINE_FILE = DATA_DIR / "quarantine.json"

# --- status constants ------------------------------------------------------- #
STATUS_PENDING = "PENDING"
STATUS_VALIDATING = "VALIDATING"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_EXPIRED = "EXPIRED"

VALID_STATUSES = frozenset(
    {STATUS_PENDING, STATUS_VALIDATING, STATUS_APPROVED, STATUS_REJECTED, STATUS_EXPIRED}
)
TERMINAL_STATUSES = frozenset({STATUS_APPROVED, STATUS_REJECTED, STATUS_EXPIRED})


# --- dataclass -------------------------------------------------------------- #
@dataclass
class QuarantineEntry:
    id: str
    worker_id: str
    task_description: str
    result_content: str
    submitted_at: str
    status: str = STATUS_PENDING
    validator_worker: Optional[str] = None
    validation_result: Optional[str] = None
    validated_at: Optional[str] = None
    expiry_minutes: int = 30

    # ---- helpers ----------------------------------------------------------- #
    def is_expired(self) -> bool:
        submitted = datetime.fromisoformat(self.submitted_at)
        elapsed = (datetime.now(timezone.utc) - submitted).total_seconds()
        return elapsed > self.expiry_minutes * 60

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "QuarantineEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# --- store ------------------------------------------------------------------ #
class QuarantineStore:
    """Thread-safe-ish JSON-backed quarantine store."""

    def __init__(self, path: Path | None = None):
        self.path = path or QUARANTINE_FILE
        self._entries: dict[str, QuarantineEntry] = {}
        self._load()

    # ---- persistence ------------------------------------------------------- #
    def _load(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                entries = raw if isinstance(raw, list) else raw.get("entries", [])
                self._entries = {
                    e["id"]: QuarantineEntry.from_dict(e) for e in entries
                }
            except (json.JSONDecodeError, KeyError):
                self._entries = {}
        else:
            self._entries = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": [e.to_dict() for e in self._entries.values()],
            "updated_at": _now_iso(),
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ---- public API -------------------------------------------------------- #
    def submit(self, worker_id: str, task: str, result: str,
               expiry_minutes: int = 30) -> str:
        if worker_id in CORE_WORKERS:
            raise ValueError(
                f"{worker_id} is a core worker -- results do not require quarantine"
            )
        entry_id = f"q_{uuid.uuid4().hex[:12]}"
        entry = QuarantineEntry(
            id=entry_id,
            worker_id=worker_id,
            task_description=task,
            result_content=result,
            submitted_at=_now_iso(),
            expiry_minutes=expiry_minutes,
        )
        self._entries[entry_id] = entry
        self._save()
        _bus_notify(
            f"QUARANTINE_SUBMIT: {entry_id} from {worker_id} -- awaiting validation",
            msg_type="quarantine_submit",
        )
        return entry_id

    def get_pending(self) -> List[QuarantineEntry]:
        self.expire_stale()
        return [e for e in self._entries.values() if e.status == STATUS_PENDING]

    def get_entry(self, entry_id: str) -> Optional[QuarantineEntry]:
        return self._entries.get(entry_id)

    def approve(self, entry_id: str, validator: str,
                notes: str = "") -> QuarantineEntry:
        entry = self._require(entry_id)
        _require_core(validator)
        _require_not_self(entry, validator)
        if entry.is_terminal():
            raise ValueError(f"Entry {entry_id} already terminal ({entry.status})")
        entry.status = STATUS_APPROVED
        entry.validator_worker = validator
        entry.validation_result = notes or "approved"
        entry.validated_at = _now_iso()
        self._save()
        _bus_notify(
            f"QUARANTINE_APPROVED: {entry_id} (worker={entry.worker_id}) "
            f"by {validator}. {notes}",
            msg_type="quarantine_approved",
        )
        return entry

    def reject(self, entry_id: str, validator: str,
               reason: str = "") -> QuarantineEntry:
        entry = self._require(entry_id)
        _require_core(validator)
        _require_not_self(entry, validator)
        if entry.is_terminal():
            raise ValueError(f"Entry {entry_id} already terminal ({entry.status})")
        entry.status = STATUS_REJECTED
        entry.validator_worker = validator
        entry.validation_result = reason or "rejected"
        entry.validated_at = _now_iso()
        self._save()
        _bus_notify(
            f"QUARANTINE_REJECTED: {entry_id} (worker={entry.worker_id}) "
            f"by {validator}. {reason}",
            msg_type="quarantine_rejected",
        )
        return entry

    def expire_stale(self) -> int:
        count = 0
        for entry in self._entries.values():
            if entry.status == STATUS_PENDING and entry.is_expired():
                entry.status = STATUS_EXPIRED
                entry.validated_at = _now_iso()
                entry.validation_result = "auto-expired"
                count += 1
        if count:
            self._save()
            _bus_notify(
                f"QUARANTINE_EXPIRED: {count} stale entries expired",
                msg_type="quarantine_expired",
            )
        return count

    def stats(self) -> dict:
        self.expire_stale()
        counts: dict[str, int] = {s: 0 for s in VALID_STATUSES}
        for entry in self._entries.values():
            counts[entry.status] = counts.get(entry.status, 0) + 1
        counts["total"] = len(self._entries)
        return counts

    # ---- internal ---------------------------------------------------------- #
    def _require(self, entry_id: str) -> QuarantineEntry:
        entry = self._entries.get(entry_id)
        if entry is None:
            raise KeyError(f"Quarantine entry {entry_id} not found")
        return entry


# --- helpers ---------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_core(worker: str) -> None:
    if worker not in CORE_WORKERS:
        raise PermissionError(
            f"{worker} is not a core worker -- only {sorted(CORE_WORKERS)} "
            f"can approve/reject quarantine entries"
        )


def _require_not_self(entry: QuarantineEntry, validator: str) -> None:
    if entry.worker_id == validator:
        raise PermissionError(
            f"{validator} cannot validate their own quarantine entry"
        )


def _bus_notify(content: str, msg_type: str = "quarantine") -> None:
    """Best-effort bus notification via guarded_publish."""
    try:
        from tools.skynet_spam_guard import guarded_publish  # type: ignore
        guarded_publish({
            "sender": "quarantine_system",
            "topic": "orchestrator",
            "type": msg_type,
            "content": content,
        })
    except Exception:
        pass  # bus down is non-fatal


# --- CLI -------------------------------------------------------------------- #
def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="External Worker Quarantine System"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # submit
    p_sub = sub.add_parser("submit", help="Submit a result to quarantine")
    p_sub.add_argument("--worker", required=True, help="Non-core worker id")
    p_sub.add_argument("--task", required=True, help="Task description")
    p_sub.add_argument("--result", required=True, help="Result content")
    p_sub.add_argument("--expiry", type=int, default=30,
                       help="Expiry in minutes (default 30)")

    # pending
    sub.add_parser("pending", help="List pending quarantine entries")

    # get
    p_get = sub.add_parser("get", help="Get a single entry by id")
    p_get.add_argument("--id", required=True, help="Entry id")

    # approve
    p_app = sub.add_parser("approve", help="Approve a quarantined entry")
    p_app.add_argument("--id", required=True, help="Entry id")
    p_app.add_argument("--validator", required=True, help="Core worker approving")
    p_app.add_argument("--notes", default="", help="Approval notes")

    # reject
    p_rej = sub.add_parser("reject", help="Reject a quarantined entry")
    p_rej.add_argument("--id", required=True, help="Entry id")
    p_rej.add_argument("--validator", required=True, help="Core worker rejecting")
    p_rej.add_argument("--reason", default="", help="Rejection reason")

    # stats
    sub.add_parser("stats", help="Show quarantine statistics")

    # expire
    sub.add_parser("expire", help="Expire stale pending entries")

    args = parser.parse_args()
    store = QuarantineStore()

    if args.command == "submit":
        try:
            eid = store.submit(args.worker, args.task, args.result, args.expiry)
            print(f"Submitted: {eid}")
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "pending":
        pending = store.get_pending()
        if not pending:
            print("No pending quarantine entries.")
        else:
            print(f"{len(pending)} pending entries:")
            for e in pending:
                age_s = (datetime.now(timezone.utc)
                         - datetime.fromisoformat(e.submitted_at)).total_seconds()
                print(f"  {e.id}  worker={e.worker_id}  "
                      f"age={int(age_s)}s  task={e.task_description[:60]}")

    elif args.command == "get":
        entry = store.get_entry(args.id)
        if entry is None:
            print(f"Entry {args.id} not found.", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(entry.to_dict(), indent=2))

    elif args.command == "approve":
        try:
            entry = store.approve(args.id, args.validator, args.notes)
            print(f"APPROVED: {entry.id} by {args.validator}")
        except (KeyError, ValueError, PermissionError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "reject":
        try:
            entry = store.reject(args.id, args.validator, args.reason)
            print(f"REJECTED: {entry.id} by {args.validator}")
        except (KeyError, ValueError, PermissionError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "stats":
        s = store.stats()
        print("Quarantine Statistics:")
        for status in sorted(VALID_STATUSES):
            print(f"  {status:12s}: {s.get(status, 0)}")
        print(f"  {'TOTAL':12s}: {s.get('total', 0)}")

    elif args.command == "expire":
        n = store.expire_stale()
        print(f"Expired {n} stale entries.")


if __name__ == "__main__":
    _cli()
