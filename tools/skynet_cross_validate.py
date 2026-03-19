"""
External Worker Cross-Validation Dispatcher.
# signed: beta

Auto-assigns core Skynet workers to verify results submitted by external
(non-core) workers.  External worker output sits in the QuarantineStore
(tools.skynet_external_quarantine) until a core worker independently
validates it.

Workflow:
    1. External worker submits result → QuarantineStore (status=PENDING)
    2. CrossValidator picks an idle core worker (never the submitter)
    3. Dispatches a structured verification task via ghost_type
    4. Core worker reviews, tests, and posts APPROVE / REJECT verdict
    5. on_validation_complete() updates quarantine entry accordingly

CLI:
    python tools/skynet_cross_validate.py --quarantine-id ID [--validator WORKER] [--auto]
    python tools/skynet_cross_validate.py --pending          # list pending entries
    python tools/skynet_cross_validate.py --listen           # daemon: auto-trigger on bus
    python tools/skynet_cross_validate.py --stats            # quarantine statistics

Rules:
    - A worker NEVER validates its own work.
    - External workers NEVER validate anything.
    - Only core workers (alpha, beta, gamma, delta) may validate.
"""
# signed: beta

from __future__ import annotations

import argparse
import json
import logging
import sys
import textwrap
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Resolve repo root so imports work when invoked from any cwd
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.skynet_spam_guard import guarded_publish  # noqa: E402

# Lazy import — Alpha may still be building this file.  We gracefully
# degrade if it is missing so the rest of the module remains importable.
try:
    from tools.skynet_external_quarantine import (  # noqa: E402
        QuarantineStore,
        CORE_WORKERS,
        STATUS_PENDING,
        STATUS_VALIDATING,
    )
except ImportError:
    QuarantineStore = None  # type: ignore[assignment,misc]
    CORE_WORKERS = frozenset({"alpha", "beta", "gamma", "delta"})
    STATUS_PENDING = "PENDING"
    STATUS_VALIDATING = "VALIDATING"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SKYNET_STATUS_URL = "http://localhost:8420/status"
SKYNET_BUS_URL = "http://localhost:8420/bus/messages"
BUS_POLL_INTERVAL_S = 10
VALIDATION_TIMEOUT_S = 300  # 5 min max wait for result
LOG = logging.getLogger("skynet.cross_validate")

# ---------------------------------------------------------------------------
# Validation task template
# ---------------------------------------------------------------------------

VALIDATION_TEMPLATE = textwrap.dedent("""\
    === CROSS-VALIDATION REQUEST ===
    You are being asked to independently verify work produced by an external worker.
    Review the task and result below, then report your verdict.

    QUARANTINE ID: {quarantine_id}
    EXTERNAL WORKER: {external_worker}
    SUBMITTED: {submitted_at}

    --- ORIGINAL TASK ---
    {task_description}

    --- EXTERNAL WORKER RESULT ---
    {result_summary}

    === YOUR INSTRUCTIONS ===
    1. Read the result carefully. Does it actually address the task?
    2. If code was produced, verify it compiles (py_compile) and looks correct.
    3. If claims are made (e.g. "tests pass"), verify them independently.
    4. Check for: correctness, completeness, security issues, fabrication.
    5. Post your verdict to the bus EXACTLY like this:

    from tools.skynet_spam_guard import guarded_publish
    guarded_publish({{
        "sender": "{validator}",
        "topic": "cross_validation",
        "type": "verdict",
        "content": "QUARANTINE {quarantine_id} VERDICT: [APPROVE or REJECT] | NOTES: <your evidence> signed:{validator}"
    }})

    RULES:
    - APPROVE only if the result is correct and complete.
    - REJECT if anything is wrong, fabricated, or incomplete. Include evidence.
    - You MUST post a verdict. Do not skip this step.
""")

# ---------------------------------------------------------------------------
# CrossValidator
# ---------------------------------------------------------------------------


class CrossValidator:
    """Orchestrates cross-validation of quarantined external worker results."""

    # signed: beta

    def __init__(self, store: "QuarantineStore | None" = None):
        if QuarantineStore is None:
            raise RuntimeError(
                "tools.skynet_external_quarantine is not available. "
                "Ensure Alpha's quarantine module is installed."
            )
        self.store: QuarantineStore = store or QuarantineStore()

    # ---- public API -------------------------------------------------------

    def request_validation(
        self,
        quarantine_id: str,
        external_worker_id: str,
        task_desc: str,
        result_summary: str,
        *,
        preferred_validator: str | None = None,
    ) -> dict:
        """End-to-end: pick a core worker and dispatch the validation task.

        Returns dict with keys:
            ok (bool), validator (str|None), quarantine_id (str),
            error (str|None), dispatch_ok (bool|None)
        """
        # signed: beta
        # 1. select validator
        exclude = [external_worker_id]
        validator = preferred_validator or self.select_validator(
            exclude_workers=exclude
        )
        if validator is None:
            return {
                "ok": False,
                "validator": None,
                "quarantine_id": quarantine_id,
                "error": "No idle core worker available for validation.",
                "dispatch_ok": None,
            }

        # 2. mark entry as VALIDATING in the store (if store supports it)
        entry = self.store.get_entry(quarantine_id)
        if entry is None:
            return {
                "ok": False,
                "validator": validator,
                "quarantine_id": quarantine_id,
                "error": f"Quarantine entry {quarantine_id} not found.",
                "dispatch_ok": None,
            }

        # Attempt to transition to VALIDATING
        try:
            if hasattr(entry, "status") and entry.status == STATUS_PENDING:
                entry.status = STATUS_VALIDATING
                entry.validator_worker = validator
                self.store._save()  # persist state change
        except Exception as exc:
            LOG.warning("Could not mark entry as VALIDATING: %s", exc)

        # 3. dispatch
        dispatch_ok = self.dispatch_validation(
            validator_name=validator,
            quarantine_id=quarantine_id,
            task_desc=task_desc,
            result_summary=result_summary,
            external_worker_id=external_worker_id,
            submitted_at=getattr(entry, "submitted_at", "unknown"),
        )

        # 4. announce on bus
        guarded_publish({
            "sender": "cross_validator",
            "topic": "cross_validation",
            "type": "dispatched",
            "content": (
                f"Validation dispatched: {quarantine_id} -> {validator} "
                f"(external worker: {external_worker_id}) signed:beta"
            ),
        })

        return {
            "ok": dispatch_ok,
            "validator": validator,
            "quarantine_id": quarantine_id,
            "error": None if dispatch_ok else "Dispatch to validator failed.",
            "dispatch_ok": dispatch_ok,
        }

    def select_validator(
        self, exclude_workers: list[str] | None = None
    ) -> str | None:
        """Pick the best idle core worker from /status.

        Selection priority:
            1. Must be a core worker (alpha/beta/gamma/delta)
            2. Must NOT be in exclude_workers
            3. Prefer IDLE status
            4. Among idle workers, prefer fewest tasks_completed (spread load)

        Returns worker name or None if nobody is available.
        """
        # signed: beta
        exclude = set(w.lower() for w in (exclude_workers or []))
        agents = _fetch_agent_status()
        if agents is None:
            LOG.warning("Cannot reach /status — falling back to round-robin")
            # Fallback: pick first core worker not in exclude list
            for w in sorted(CORE_WORKERS):
                if w not in exclude:
                    return w
            return None

        candidates: list[tuple[str, dict]] = []
        for name, info in agents.items():
            name_l = name.lower()
            if name_l not in CORE_WORKERS:
                continue
            if name_l in exclude:
                continue
            candidates.append((name_l, info))

        if not candidates:
            return None

        # Sort: IDLE first, then by tasks_completed ascending (spread load)
        def _score(item: tuple[str, dict]) -> tuple[int, int]:
            _name, info = item
            status = (info.get("status") or "").upper()
            is_idle = 0 if status == "IDLE" else 1
            completed = info.get("tasks_completed", 0)
            return (is_idle, completed)

        candidates.sort(key=_score)
        return candidates[0][0]

    def dispatch_validation(
        self,
        validator_name: str,
        quarantine_id: str,
        task_desc: str,
        result_summary: str,
        external_worker_id: str = "unknown",
        submitted_at: str = "unknown",
    ) -> bool:
        """Dispatch the structured verification task to a core worker.

        Uses skynet_dispatch.dispatch_to_worker for ghost-type delivery.
        Falls back to bus directive if dispatch module is unavailable.

        Returns True on success.
        """
        # signed: beta
        prompt = VALIDATION_TEMPLATE.format(
            quarantine_id=quarantine_id,
            external_worker=external_worker_id,
            submitted_at=submitted_at,
            task_description=task_desc,
            result_summary=_truncate(result_summary, 3000),
            validator=validator_name,
        )

        # Try ghost-type dispatch first (most reliable)
        try:
            from tools.skynet_dispatch import dispatch_to_worker  # noqa: E402

            ok = dispatch_to_worker(validator_name, prompt)
            if ok:
                LOG.info(
                    "Dispatched CV task %s -> %s via ghost_type",
                    quarantine_id, validator_name,
                )
                return True
            LOG.warning(
                "ghost_type dispatch failed for %s -> %s, trying bus directive",
                quarantine_id, validator_name,
            )
        except Exception as exc:
            LOG.warning("dispatch_to_worker unavailable: %s", exc)

        # Fallback: post directive on bus for the worker to pick up
        result = guarded_publish({
            "sender": "cross_validator",
            "topic": "workers",
            "type": "directive",
            "content": prompt,
            "metadata": {"target": validator_name},
        })
        return bool(result and result.get("allowed"))

    def on_validation_complete(
        self,
        quarantine_id: str,
        validator: str,
        verdict: str,
        notes: str = "",
    ) -> dict:
        """Process a completed validation verdict.

        Updates quarantine store and posts result to bus.

        Args:
            quarantine_id: The quarantine entry ID
            validator: Core worker who validated
            verdict: "APPROVE" or "REJECT"
            notes: Evidence / reasoning

        Returns dict with ok, status, quarantine_id.
        """
        # signed: beta
        verdict_upper = verdict.strip().upper()
        if verdict_upper not in ("APPROVE", "REJECT"):
            return {
                "ok": False,
                "status": "INVALID_VERDICT",
                "quarantine_id": quarantine_id,
                "error": f"Verdict must be APPROVE or REJECT, got: {verdict}",
            }

        entry = self.store.get_entry(quarantine_id)
        if entry is None:
            return {
                "ok": False,
                "status": "NOT_FOUND",
                "quarantine_id": quarantine_id,
                "error": f"Entry {quarantine_id} not found in quarantine store.",
            }

        try:
            if verdict_upper == "APPROVE":
                self.store.approve(quarantine_id, validator, notes=notes)
                new_status = "APPROVED"
            else:
                self.store.reject(quarantine_id, validator, reason=notes)
                new_status = "REJECTED"
        except (ValueError, PermissionError) as exc:
            return {
                "ok": False,
                "status": "ERROR",
                "quarantine_id": quarantine_id,
                "error": str(exc),
            }

        # Announce on bus
        guarded_publish({
            "sender": "cross_validator",
            "topic": "cross_validation",
            "type": "completed",
            "content": (
                f"Quarantine {quarantine_id} {new_status} by {validator}. "
                f"Notes: {_truncate(notes, 200)} signed:beta"
            ),
        })

        LOG.info(
            "Quarantine %s %s by %s", quarantine_id, new_status, validator
        )
        return {
            "ok": True,
            "status": new_status,
            "quarantine_id": quarantine_id,
        }


# ---------------------------------------------------------------------------
# Bus listener — auto-triggers validation for new external worker results
# ---------------------------------------------------------------------------


def listen_for_external_results(cv: CrossValidator, poll_interval: int = BUS_POLL_INTERVAL_S) -> None:
    """Daemon loop: poll bus for external worker results and auto-dispatch CV.

    Watches for messages matching:
        sender NOT in CORE_WORKERS, type=result

    Also watches for validator verdicts:
        topic=cross_validation, type=verdict
    """
    # signed: beta
    seen_ids: set[str] = set()
    seen_verdicts: set[str] = set()
    print(f"[cross_validate] Listening for external results (poll={poll_interval}s)...")

    while True:
        try:
            messages = _fetch_bus_messages(limit=50)
            if messages is None:
                time.sleep(poll_interval)
                continue

            for msg in messages:
                msg_id = msg.get("id", "")
                sender = (msg.get("sender") or "").lower()
                msg_type = (msg.get("type") or "").lower()
                topic = (msg.get("topic") or "").lower()
                content = msg.get("content") or ""

                # --- Handle new external worker results ---
                if (
                    msg_id not in seen_ids
                    and sender not in CORE_WORKERS
                    and sender not in ("orchestrator", "system", "cross_validator", "monitor")
                    and msg_type == "result"
                ):
                    seen_ids.add(msg_id)
                    print(f"[cross_validate] External result from '{sender}': {_truncate(content, 80)}")

                    # Submit to quarantine if not already there
                    try:
                        entry_id = cv.store.submit(
                            worker_id=sender,
                            task=f"External result from {sender}",
                            result=content,
                            expiry_minutes=30,
                        )
                        print(f"[cross_validate] Quarantined as {entry_id}")
                    except ValueError:
                        # Might be a core worker or already quarantined
                        continue

                    # Auto-dispatch validation
                    result = cv.request_validation(
                        quarantine_id=entry_id,
                        external_worker_id=sender,
                        task_desc=f"External result from {sender}",
                        result_summary=content,
                    )
                    if result["ok"]:
                        print(f"[cross_validate] Dispatched to {result['validator']}")
                    else:
                        print(f"[cross_validate] Dispatch failed: {result.get('error')}")

                # --- Handle validator verdicts ---
                if (
                    msg_id not in seen_verdicts
                    and topic == "cross_validation"
                    and msg_type == "verdict"
                    and sender in CORE_WORKERS
                ):
                    seen_verdicts.add(msg_id)
                    _process_verdict_message(cv, sender, content)

        except KeyboardInterrupt:
            print("\n[cross_validate] Stopped.")
            break
        except Exception as exc:
            LOG.error("Listen loop error: %s", exc)

        time.sleep(poll_interval)


def _process_verdict_message(cv: CrossValidator, validator: str, content: str) -> None:
    """Parse a verdict bus message and update quarantine store.

    Expected content format:
        QUARANTINE <id> VERDICT: APPROVE|REJECT | NOTES: <text> signed:<worker>
    """
    # signed: beta
    import re

    match = re.search(
        r"QUARANTINE\s+(q_\w+)\s+VERDICT:\s*(APPROVE|REJECT)\s*\|?\s*NOTES:\s*(.*?)(?:\s+signed:|\Z)",
        content,
        re.IGNORECASE,
    )
    if not match:
        LOG.warning("Could not parse verdict from %s: %s", validator, _truncate(content, 100))
        return

    qid = match.group(1)
    verdict = match.group(2).upper()
    notes = match.group(3).strip()

    result = cv.on_validation_complete(qid, validator, verdict, notes)
    if result["ok"]:
        print(f"[cross_validate] {qid} -> {result['status']} by {validator}")
    else:
        print(f"[cross_validate] Failed to update {qid}: {result.get('error')}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_agent_status() -> dict | None:
    """GET /status and return the agents dict, or None on error."""
    # signed: beta
    try:
        req = urllib.request.Request(SKYNET_STATUS_URL, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        return data.get("agents", {})
    except Exception as exc:
        LOG.debug("Failed to fetch /status: %s", exc)
        return None


def _fetch_bus_messages(limit: int = 30) -> list | None:
    """GET /bus/messages and return the list, or None on error."""
    # signed: beta
    try:
        url = f"{SKYNET_BUS_URL}?limit={limit}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        # Backend returns list directly or {"messages": [...]}
        if isinstance(data, list):
            return data
        return data.get("messages", [])
    except Exception as exc:
        LOG.debug("Failed to fetch bus messages: %s", exc)
        return None


def _truncate(text: str, max_len: int = 200) -> str:
    """Truncate text to max_len chars with ellipsis."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_pending(cv: CrossValidator) -> None:
    """Display pending quarantine entries."""
    # signed: beta
    pending = cv.store.get_pending()
    if not pending:
        print("No pending quarantine entries.")
        return
    print(f"\n{'ID':<16} {'Worker':<12} {'Status':<12} {'Task':<40}")
    print("-" * 80)
    for entry in pending:
        print(
            f"{entry.id:<16} {entry.worker_id:<12} {entry.status:<12} "
            f"{_truncate(entry.task_description, 40)}"
        )
    print(f"\nTotal pending: {len(pending)}")


def _print_stats(cv: CrossValidator) -> None:
    """Display quarantine statistics."""
    # signed: beta
    stats = cv.store.stats()
    print("\n=== Quarantine Statistics ===")
    for key, val in stats.items():
        print(f"  {key:<12}: {val}")


def main() -> None:
    """CLI entry point."""
    # signed: beta
    parser = argparse.ArgumentParser(
        description="External Worker Cross-Validation Dispatcher"
    )
    parser.add_argument(
        "--quarantine-id", "-q",
        type=str,
        help="Quarantine entry ID to validate",
    )
    parser.add_argument(
        "--validator", "-v",
        type=str,
        help="Specific core worker to assign (default: auto-select)",
    )
    parser.add_argument(
        "--auto", "-a",
        action="store_true",
        help="Auto-select best idle core worker and dispatch",
    )
    parser.add_argument(
        "--pending", "-p",
        action="store_true",
        help="List pending quarantine entries",
    )
    parser.add_argument(
        "--listen", "-l",
        action="store_true",
        help="Daemon mode: auto-trigger validation on external worker results",
    )
    parser.add_argument(
        "--stats", "-s",
        action="store_true",
        help="Show quarantine statistics",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=BUS_POLL_INTERVAL_S,
        help=f"Bus poll interval in seconds (default: {BUS_POLL_INTERVAL_S})",
    )

    args = parser.parse_args()

    if QuarantineStore is None:
        print(
            "ERROR: tools.skynet_external_quarantine not found. "
            "Ensure the quarantine module is installed.",
            file=sys.stderr,
        )
        sys.exit(1)

    cv = CrossValidator()

    # --- Mode dispatch ---
    if args.pending:
        _print_pending(cv)
        return

    if args.stats:
        _print_stats(cv)
        return

    if args.listen:
        listen_for_external_results(cv, poll_interval=args.poll_interval)
        return

    if args.quarantine_id:
        entry = cv.store.get_entry(args.quarantine_id)
        if entry is None:
            print(f"ERROR: Quarantine entry {args.quarantine_id} not found.", file=sys.stderr)
            sys.exit(1)

        if args.auto or args.validator:
            result = cv.request_validation(
                quarantine_id=entry.id,
                external_worker_id=entry.worker_id,
                task_desc=entry.task_description,
                result_summary=entry.result_content,
                preferred_validator=args.validator,
            )
            if result["ok"]:
                print(f"Validation dispatched to {result['validator']} for {entry.id}")
            else:
                print(f"Failed: {result.get('error')}", file=sys.stderr)
                sys.exit(1)
        else:
            # Just show the entry details
            print(f"\nQuarantine Entry: {entry.id}")
            print(f"  Worker:    {entry.worker_id}")
            print(f"  Status:    {entry.status}")
            print(f"  Validator: {entry.validator_worker or 'none'}")
            print(f"  Submitted: {entry.submitted_at}")
            print(f"  Task:      {_truncate(entry.task_description, 60)}")
            print(f"  Result:    {_truncate(entry.result_content, 60)}")
            print(f"\nUse --auto or --validator WORKER to dispatch validation.")
        return

    # No action specified — show help
    parser.print_help()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    main()
