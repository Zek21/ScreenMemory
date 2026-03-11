"""Skynet Worker Scoring System.

Tracks cross-validated task completions, bug validation, ticket awareness,
zero-ticket completion bonuses, and refactor-review penalties. Workers earn
points when their work or findings are validated by a *different* actor.

Scoring defaults:
  - award:                        +0.01 pts per cross-validated task
  - deduct:                       -0.005 pts per failed validation
  - bug filed:                    +0.01 pts for filing a bug for
                                  cross-validation
  - bug confirmation:             +0.01 pts to the original filer when an
                                  unbiased validator proves it true
  - bug cross-validation:         +0.01 pts to the independent validator
  - refactor deduction:           -0.01 pts when a worker performs a refactor
  - refactor reversal:            +0.01 pts when an unbiased validator
                                  confirms the refactor was necessary
  - biased refactor report:       -0.10 pts if the refactoring worker gave a
                                  biased self-serving report
  - proactive ticket clear:       +0.20 pts when an orchestrator/consultant
                                  proactively clears or surfaces a Skynet ticket
  - autonomous pull:              +0.20 pts when a worker pulls the next
                                  ticket without waiting to be spoon-fed
  - zero-ticket completion bonus: +1.00 pts to orchestrator when the queue
                                  truly reaches zero, and +1.00 pts to the
                                  actor that closed the final ticket

Data:  data/worker_scores.json
Bus:   Posts score changes to topic=scoring on localhost:8420

CLI:
  python tools/skynet_scoring.py --award WORKER --task-id TID --validator VNAME
  python tools/skynet_scoring.py --deduct WORKER --task-id TID --validator VNAME
  python tools/skynet_scoring.py --file-bug WORKER --task-id TID [--validator VNAME]
  python tools/skynet_scoring.py --confirm-bug WORKER --task-id TID --validator VNAME
  python tools/skynet_scoring.py --refactor WORKER --task-id TID [--validator VNAME]
  python tools/skynet_scoring.py --refactor-necessary WORKER --task-id TID --validator VNAME
  python tools/skynet_scoring.py --biased-refactor-report WORKER --task-id TID --validator VNAME
  python tools/skynet_scoring.py --proactive-ticket-clear ACTOR --task-id TID [--validator VNAME]
  python tools/skynet_scoring.py --autonomous-pull WORKER --task-id TID [--validator VNAME]
  python tools/skynet_scoring.py --zero-ticket-bonus --task-id TID --last-worker WORKER [--validator VNAME]
  python tools/skynet_scoring.py --leaderboard
  python tools/skynet_scoring.py --history [WORKER]
  python tools/skynet_scoring.py --score WORKER
"""

import argparse
import json
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SCORES_FILE = DATA_DIR / "worker_scores.json"
_lock = threading.Lock()  # signed: beta
BRAIN_CONFIG_FILE = DATA_DIR / "brain_config.json"
BUS_URL = "http://localhost:8420/bus/publish"
SCHEMA_VERSION = 5

DEFAULT_AWARD = 0.01
DEFAULT_DEDUCT = 0.005
DEFAULT_BUG_REPORT_AWARD = 0.01
DEFAULT_BUG_REPORT_CONFIRMATION_AWARD = 0.01
DEFAULT_BUG_CROSS_VALIDATION_AWARD = 0.01
DEFAULT_REFACTOR_DEDUCT = 0.01
DEFAULT_REFACTOR_NECESSARY_REVERSAL = 0.01
DEFAULT_BIASED_REFACTOR_REPORT_DEDUCT = 0.1
DEFAULT_PROACTIVE_TICKET_CLEAR_AWARD = 0.2
DEFAULT_AUTONOMOUS_PULL_AWARD = 0.2
DEFAULT_TICKET_ZERO_BONUS_AWARD = 1.0

_lock = threading.Lock()  # Guards all read-modify-write cycles on SCORES_FILE  # signed: gamma


def _empty_store() -> dict:
    return {"version": SCHEMA_VERSION, "scores": {}, "history": []}


def _load_protocol() -> dict:
    protocol = {
        "award_per_task": DEFAULT_AWARD,
        "failed_validation_deduction": DEFAULT_DEDUCT,
        "bug_report_award": DEFAULT_BUG_REPORT_AWARD,
        "bug_report_confirmation_award": DEFAULT_BUG_REPORT_CONFIRMATION_AWARD,
        "bug_cross_validation_award": DEFAULT_BUG_CROSS_VALIDATION_AWARD,
        "refactor_deduction": DEFAULT_REFACTOR_DEDUCT,
        "refactor_necessary_reversal": DEFAULT_REFACTOR_NECESSARY_REVERSAL,
        "biased_refactor_report_deduction": DEFAULT_BIASED_REFACTOR_REPORT_DEDUCT,
        "proactive_ticket_clear_award": DEFAULT_PROACTIVE_TICKET_CLEAR_AWARD,
        "autonomous_pull_award": DEFAULT_AUTONOMOUS_PULL_AWARD,
        "ticket_zero_bonus_award": DEFAULT_TICKET_ZERO_BONUS_AWARD,
        "require_independent_refactor_validation": True,
    }
    if not BRAIN_CONFIG_FILE.exists():
        return protocol
    try:
        raw = json.loads(BRAIN_CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return protocol
    scoring = raw.get("dispatch_rules", {}).get("scoring_protocol", {})
    if not isinstance(scoring, dict):
        return protocol
    for key in (
        "award_per_task",
        "failed_validation_deduction",
        "bug_report_award",
        "bug_report_confirmation_award",
        "bug_cross_validation_award",
        "refactor_deduction",
        "refactor_necessary_reversal",
        "biased_refactor_report_deduction",
        "proactive_ticket_clear_award",
        "autonomous_pull_award",
        "ticket_zero_bonus_award",
    ):
        if key in scoring:
            protocol[key] = float(scoring[key])
    if "require_independent_refactor_validation" in scoring:
        protocol["require_independent_refactor_validation"] = bool(
            scoring["require_independent_refactor_validation"]
        )
    return protocol


def _normalize_worker_entry(entry: dict | None) -> dict:
    entry = dict(entry or {})
    entry.setdefault("total", 0.0)
    entry.setdefault("awards", 0)
    entry.setdefault("deductions", 0)
    entry.setdefault("refactor_deductions", 0)
    entry.setdefault("refactor_reversals", 0)
    entry.setdefault("bias_penalties", 0)
    entry.setdefault("proactive_ticket_clears", 0)
    entry.setdefault("autonomous_pull_awards", 0)
    entry.setdefault("bug_reports_filed", 0)
    entry.setdefault("bug_report_confirmations", 0)
    entry.setdefault("bug_cross_validations", 0)
    entry.setdefault("zero_ticket_bonus_awards", 0)
    return entry


def _normalize_store(raw: dict | None) -> dict:
    if not isinstance(raw, dict):
        return _empty_store()

    if "scores" in raw and "history" in raw:
        data = {
            "version": SCHEMA_VERSION,
            "scores": raw.get("scores", {}),
            "history": raw.get("history", []),
        }
    else:
        data = _empty_store()
        for worker, legacy in raw.items():
            if not isinstance(legacy, dict):
                continue
            data["scores"][worker] = {
                "total": 0.0,
                "awards": legacy.get("success", 0),
                "deductions": legacy.get("fail", 0),
                "refactor_deductions": 0,
                "refactor_reversals": 0,
                "bias_penalties": 0,
                "legacy_stats": {
                    "task_count": legacy.get("total", 0),
                    "success_rate": legacy.get("success_rate", 0),
                    "avg_time": legacy.get("avg_time", 0),
                },
            }

    data["scores"] = {
        worker: _normalize_worker_entry(entry)
        for worker, entry in data.get("scores", {}).items()
        if isinstance(entry, dict)
    }
    history = data.get("history", [])
    data["history"] = history if isinstance(history, list) else []
    data["version"] = SCHEMA_VERSION
    return data


def _load() -> dict:
    """Load scores data from disk, migrating legacy format if needed."""
    if SCORES_FILE.exists():
        try:
            raw = json.loads(SCORES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return _empty_store()
        return _normalize_store(raw)

    return _empty_store()


def _save(data: dict) -> None:
    """Persist scores data to disk atomically to prevent corruption."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from tools.skynet_atomic import atomic_write_json
        atomic_write_json(SCORES_FILE, data)
    except (ModuleNotFoundError, ImportError):
        # Fallback: manual atomic write via temp+replace
        tmp = SCORES_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )
        tmp.replace(SCORES_FILE)
    # signed: gamma


def _bus_post(msg: dict) -> bool:
    """Post a message to the Skynet bus. Returns True on success."""
    try:
        payload = json.dumps(msg).encode()
        req = urllib.request.Request(
            BUS_URL, payload, {"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def _ensure_worker(data: dict, worker: str) -> None:
    """Initialize a worker entry if it doesn't exist."""
    if worker not in data["scores"]:
        data["scores"][worker] = _normalize_worker_entry({})
    else:
        data["scores"][worker] = _normalize_worker_entry(data["scores"][worker])


def _require_independent_validator(worker: str, validator: str, action: str) -> None:
    if worker == validator:
        raise ValueError(
            f"Independent validation required: {worker} cannot {action} own work"
        )


def _append_record(data: dict, record: dict, msg_type: str) -> None:
    """Append a record and save. Caller MUST hold _lock."""  # signed: gamma
    data["history"].append(record)
    _save(data)
    _bus_post({
        "sender": "scoring",
        "topic": "scoring",
        "type": msg_type,
        "content": json.dumps(record),
    })


def _refactor_history(data: dict, worker: str, task_id: str) -> list[dict]:
    return [
        r for r in data.get("history", [])
        if r.get("worker") == worker and r.get("task_id") == task_id
    ]


def _pending_refactor_reversal_count(data: dict, worker: str, task_id: str) -> int:
    relevant = _refactor_history(data, worker, task_id)
    deducted = sum(1 for r in relevant if r.get("action") == "refactor_deduct")
    reversed_count = sum(1 for r in relevant if r.get("action") == "refactor_reversal")
    return deducted - reversed_count


def _bug_report_history(data: dict, worker: str, task_id: str) -> list[dict]:
    return [
        r for r in data.get("history", [])
        if r.get("worker") == worker
        and r.get("task_id") == task_id
        and str(r.get("protocol", "")).startswith("bug_cross_validation")
    ]


def _bug_report_exists(data: dict, worker: str, task_id: str) -> bool:
    return any(r.get("action") == "bug_report_filed" for r in _bug_report_history(data, worker, task_id))


def _pending_bug_confirmation_count(data: dict, worker: str, task_id: str) -> int:
    relevant = _bug_report_history(data, worker, task_id)
    filed = sum(1 for r in relevant if r.get("action") == "bug_report_filed")
    confirmed = sum(1 for r in relevant if r.get("action") == "bug_report_confirmed")
    return filed - confirmed


def _zero_ticket_bonus_exists(data: dict, task_id: str) -> bool:
    return any(
        r.get("task_id") == task_id and r.get("protocol") == "ticket_zero_completion"
        for r in data.get("history", [])
    )


def _all_tickets_cleared() -> bool:
    try:
        from tools import skynet_todos as todos
    except ModuleNotFoundError:
        import skynet_todos as todos
    try:
        return bool(todos.all_tickets_cleared())
    except Exception:
        return False


def award_points(
    worker: str,
    task_id: str,
    validator: str,
    amount: float = DEFAULT_AWARD,
) -> dict:
    """Award points to a worker for a cross-validated task.

    Args:
        worker: The worker being rewarded.
        task_id: Identifier of the validated task.
        validator: The worker who validated the task (must differ from worker).
        amount: Points to award (default 0.01).

    Returns:
        Updated score entry for the worker.
    """
    _require_independent_validator(worker, validator, "validate")

    with _lock:  # signed: gamma
        data = _load()
        _ensure_worker(data, worker)

        entry = data["scores"][worker]
        entry["total"] = round(entry["total"] + amount, 6)
        entry["awards"] += 1

        record = {
            "worker": worker,
            "action": "award",
            "amount": amount,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": entry["total"],
        }
        _append_record(data, record, "award")

    return entry


DISPATCH_LOG_FILE = DATA_DIR / "dispatch_log.json"  # signed: alpha


def verify_dispatch_evidence(worker: str, task_id: str) -> dict:
    """Verify that a task was actually dispatched to a worker before allowing deductions.

    Reads data/dispatch_log.json and searches for a dispatch entry matching
    the worker name AND task_summary (fuzzy match on first 50 chars).

    Returns:
        dict with keys: verified, dispatch_found, dispatch_success,
        dispatch_time, result_received, worker_state_at_dispatch
    """
    result = {
        "verified": False,
        "dispatch_found": False,
        "dispatch_success": False,
        "dispatch_time": "",
        "result_received": False,
        "worker_state_at_dispatch": "",
    }
    try:
        if not DISPATCH_LOG_FILE.exists():
            return result
        entries = json.loads(DISPATCH_LOG_FILE.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            return result

        task_prefix = task_id[:50].lower() if task_id else ""
        for entry in reversed(entries):
            entry_worker = entry.get("worker", "").lower()
            entry_summary = (entry.get("task_summary", "") or "")[:50].lower()
            if entry_worker == worker.lower() and (
                not task_prefix or task_prefix in entry_summary
                or entry_summary in task_prefix
            ):
                result["dispatch_found"] = True
                result["dispatch_success"] = bool(entry.get("success", False))
                result["dispatch_time"] = entry.get("timestamp", "")
                result["result_received"] = bool(entry.get("result_received", False))
                result["worker_state_at_dispatch"] = entry.get("state_at_dispatch", "")

                if not result["dispatch_success"]:
                    result["verified"] = False
                elif result["result_received"]:
                    # Worker DID deliver -- no deduction allowed
                    result["verified"] = False
                else:
                    # Dispatch succeeded, no result yet -- legitimate deduction
                    result["verified"] = True
                return result
    except (json.JSONDecodeError, OSError, KeyError):
        pass
    return result
    # signed: alpha


def deduct_points(
    worker: str,
    task_id: str,
    validator: str,
    amount: float = DEFAULT_DEDUCT,
    force: bool = False,
) -> dict:
    """Deduct points from a worker for a failed validation.

    Args:
        worker: The worker being penalized.
        task_id: Identifier of the failed task.
        validator: The worker who found the failure.
        amount: Points to deduct (default 0.005).
        force: If True, skip dispatch evidence check (for system-level
               penalties like spam_guard or process violations).

    Returns:
        Updated score entry for the worker, or None if deduction rejected.
    """
    _require_independent_validator(worker, validator, "penalize")

    # Dispatch evidence check -- reject unverified deductions unless forced
    evidence = None
    if not force:
        evidence = verify_dispatch_evidence(worker, task_id)
        if not evidence["verified"]:
            import sys
            print(
                f"[scoring] REJECTED deduction of {amount} from {worker}: "
                f"dispatch evidence not verified. "
                f"found={evidence['dispatch_found']}, "
                f"success={evidence['dispatch_success']}, "
                f"result_received={evidence['result_received']}",
                file=sys.stderr,
            )
            return None
    # signed: alpha

    with _lock:  # signed: gamma
        data = _load()
        _ensure_worker(data, worker)

        entry = data["scores"][worker]
        entry["total"] = round(entry["total"] - amount, 6)
        entry["deductions"] += 1

        record = {
            "worker": worker,
            "action": "deduct",
            "amount": amount,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": entry["total"],
            "forced": force,
        }
        if evidence is not None:
            record["dispatch_evidence"] = evidence
        _append_record(data, record, "deduct")

    return entry
    # signed: alpha


def _award_bonus(
    actor: str,
    task_id: str,
    validator: str,
    amount: float,
    action: str,
    counter_key: str,
    protocol_name: str,
    finding: str,
) -> dict:
    _require_independent_validator(actor, validator, f"validate {action} for")

    with _lock:  # signed: gamma
        data = _load()
        _ensure_worker(data, actor)

        entry = data["scores"][actor]
        entry["total"] = round(entry["total"] + amount, 6)
        entry["awards"] += 1
        entry[counter_key] += 1

        record = {
            "worker": actor,
            "action": action,
            "amount": amount,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": entry["total"],
            "protocol": protocol_name,
            "finding": finding,
        }
        _append_record(data, record, action)
    return entry


def deduct_for_refactor(
    worker: str,
    task_id: str,
    validator: str = "orchestrator",
    amount: float | None = None,
) -> dict:
    """Apply the baseline refactor deduction."""
    protocol = _load_protocol()
    deduction = protocol["refactor_deduction"] if amount is None else amount

    with _lock:  # signed: gamma
        data = _load()
        _ensure_worker(data, worker)
        entry = data["scores"][worker]
        entry["total"] = round(entry["total"] - deduction, 6)
        entry["deductions"] += 1
        entry["refactor_deductions"] += 1

        record = {
            "worker": worker,
            "action": "refactor_deduct",
            "amount": deduction,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": entry["total"],
            "protocol": "refactor_review",
            "finding": "refactor_performed",
        }
        _append_record(data, record, "refactor_deduct")
    return entry


def cancel_refactor_deduction_if_necessary(
    worker: str,
    task_id: str,
    validator: str,
    amount: float | None = None,
) -> dict:
    """Reverse the refactor deduction when an unbiased validator says it was necessary."""
    protocol = _load_protocol()
    if protocol["require_independent_refactor_validation"]:
        _require_independent_validator(worker, validator, "validate necessity for")

    reversal = (
        protocol["refactor_necessary_reversal"]
        if amount is None else amount
    )
    with _lock:  # signed: gamma
        data = _load()
        _ensure_worker(data, worker)

        if _pending_refactor_reversal_count(data, worker, task_id) <= 0:
            raise ValueError(
                f"No uncancelled refactor deduction found for {worker} task {task_id}"
            )

        entry = data["scores"][worker]
        entry["total"] = round(entry["total"] + reversal, 6)
        entry["awards"] += 1
        entry["refactor_reversals"] += 1

        record = {
            "worker": worker,
            "action": "refactor_reversal",
            "amount": reversal,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": entry["total"],
            "protocol": "refactor_review",
            "finding": "necessary_refactor_confirmed",
            "unbiased": True,
        }
        _append_record(data, record, "refactor_reversal")
    return entry


def deduct_for_biased_refactor_report(
    worker: str,
    task_id: str,
    validator: str,
    amount: float | None = None,
) -> dict:
    """Penalize a worker for giving a biased report about their own refactor."""
    protocol = _load_protocol()
    if protocol["require_independent_refactor_validation"]:
        _require_independent_validator(worker, validator, "validate bias for")

    penalty = (
        protocol["biased_refactor_report_deduction"]
        if amount is None else amount
    )
    with _lock:  # signed: gamma
        data = _load()
        _ensure_worker(data, worker)
        if not _refactor_history(data, worker, task_id):
            raise ValueError(f"No refactor history found for {worker} task {task_id}")

        entry = data["scores"][worker]
        entry["total"] = round(entry["total"] - penalty, 6)
        entry["deductions"] += 1
        entry["bias_penalties"] += 1

        record = {
            "worker": worker,
            "action": "biased_refactor_report",
            "amount": penalty,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": entry["total"],
            "protocol": "refactor_review",
            "finding": "biased_self_report",
            "biased_reporter": worker,
            "unbiased_validator": validator,
        }
        _append_record(data, record, "biased_refactor_report")
    return entry


def award_proactive_ticket_clear(
    actor: str,
    task_id: str,
    validator: str = "god",
    amount: float | None = None,
) -> dict:
    """Reward proactive ticket clearing/surfacing by orchestrator or consultants."""
    protocol = _load_protocol()
    reward = (
        protocol["proactive_ticket_clear_award"]
        if amount is None else amount
    )
    return _award_bonus(
        actor=actor,
        task_id=task_id,
        validator=validator,
        amount=reward,
        action="proactive_ticket_clear",
        counter_key="proactive_ticket_clears",
        protocol_name="ticket_awareness",
        finding="proactive_ticket_clear",
    )


def award_autonomous_pull(
    worker: str,
    task_id: str,
    validator: str = "orchestrator",
    amount: float | None = None,
) -> dict:
    """Reward a worker for autonomously pulling the next ticket."""
    protocol = _load_protocol()
    reward = (
        protocol["autonomous_pull_award"]
        if amount is None else amount
    )
    return _award_bonus(
        actor=worker,
        task_id=task_id,
        validator=validator,
        amount=reward,
        action="autonomous_pull_award",
        counter_key="autonomous_pull_awards",
        protocol_name="ticket_awareness",
        finding="autonomous_next_ticket_pull",
    )


def award_bug_report(
    worker: str,
    task_id: str,
    validator: str = "orchestrator",
    amount: float | None = None,
) -> dict:
    """Reward a worker for filing a real bug for cross-validation."""
    protocol = _load_protocol()
    reward = protocol["bug_report_award"] if amount is None else amount
    _require_independent_validator(worker, validator, "acknowledge bug report for")

    with _lock:  # signed: gamma
        data = _load()
        if _bug_report_exists(data, worker, task_id):
            raise ValueError(f"Bug report already filed for {worker} task {task_id}")

        _ensure_worker(data, worker)
        entry = data["scores"][worker]
        entry["total"] = round(entry["total"] + reward, 6)
        entry["awards"] += 1
        entry["bug_reports_filed"] += 1

        record = {
            "worker": worker,
            "action": "bug_report_filed",
            "amount": reward,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": entry["total"],
            "protocol": "bug_cross_validation:file",
            "finding": "bug_filed_for_cross_validation",
        }
        _append_record(data, record, "bug_report_filed")
    return entry


def confirm_bug_report(
    worker: str,
    task_id: str,
    validator: str,
    amount: float | None = None,
) -> dict:
    """Reward the reporter and independent validator when a bug is proven true."""
    protocol = _load_protocol()
    reporter_reward = (
        protocol["bug_report_confirmation_award"] if amount is None else amount
    )
    validator_reward = (
        protocol["bug_cross_validation_award"] if amount is None else amount
    )
    _require_independent_validator(worker, validator, "confirm bug report for")

    with _lock:  # signed: gamma
        data = _load()
        if _pending_bug_confirmation_count(data, worker, task_id) <= 0:
            raise ValueError(
                f"No pending filed bug report found for {worker} task {task_id}"
            )

        _ensure_worker(data, worker)
        _ensure_worker(data, validator)

        reporter_entry = data["scores"][worker]
        reporter_entry["total"] = round(reporter_entry["total"] + reporter_reward, 6)
        reporter_entry["awards"] += 1
        reporter_entry["bug_report_confirmations"] += 1

        reporter_record = {
            "worker": worker,
            "action": "bug_report_confirmed",
            "amount": reporter_reward,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": reporter_entry["total"],
            "protocol": "bug_cross_validation:confirm",
            "finding": "reported_bug_proven_true",
            "reported_by": worker,
        }
        _append_record(data, reporter_record, "bug_report_confirmed")

        validator_entry = data["scores"][validator]
        validator_entry["total"] = round(validator_entry["total"] + validator_reward, 6)
        validator_entry["awards"] += 1
        validator_entry["bug_cross_validations"] += 1

        validator_record = {
            "worker": validator,
            "action": "bug_cross_validation_award",
            "amount": validator_reward,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": validator_entry["total"],
            "protocol": "bug_cross_validation:validator",
            "finding": "independent_bug_confirmation",
            "reported_by": worker,
        }
        _append_record(data, validator_record, "bug_cross_validation_award")
    return {"reporter": reporter_entry, "validator": validator_entry}


def award_zero_ticket_clear(
    task_id: str,
    last_worker: str,
    validator: str = "god",
    amount: float | None = None,
) -> dict:
    """Award the orchestrator and final ticket closer when the queue hits zero."""
    protocol = _load_protocol()
    reward = protocol["ticket_zero_bonus_award"] if amount is None else amount
    actor = str(last_worker or "").strip().lower()
    if not actor:
        raise ValueError("last_worker is required for zero-ticket bonus")
    if not _all_tickets_cleared():
        raise ValueError("Zero-ticket bonus denied: open TODO items remain")

    with _lock:  # signed: gamma
        data = _load()
        if _zero_ticket_bonus_exists(data, task_id):
            raise ValueError(f"Zero-ticket bonus already recorded for task {task_id}")

        _require_independent_validator("orchestrator", validator, "validate zero-ticket bonus for")
        _ensure_worker(data, "orchestrator")
        orchestrator_entry = data["scores"]["orchestrator"]
        orchestrator_entry["total"] = round(orchestrator_entry["total"] + reward, 6)
        orchestrator_entry["awards"] += 1
        orchestrator_entry["zero_ticket_bonus_awards"] += 1

        orchestrator_record = {
            "worker": "orchestrator",
            "action": "zero_ticket_bonus",
            "amount": reward,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": orchestrator_entry["total"],
            "protocol": "ticket_zero_completion",
            "finding": "all_tickets_cleared",
            "last_worker": actor,
        }
        _append_record(data, orchestrator_record, "zero_ticket_bonus")

        _require_independent_validator(actor, validator, "validate zero-ticket bonus for")
        _ensure_worker(data, actor)
        actor_entry = data["scores"][actor]
        actor_entry["total"] = round(actor_entry["total"] + reward, 6)
        actor_entry["awards"] += 1
        actor_entry["zero_ticket_bonus_awards"] += 1

        actor_record = {
            "worker": actor,
            "action": "zero_ticket_bonus",
            "amount": reward,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": actor_entry["total"],
            "protocol": "ticket_zero_completion",
            "finding": "last_ticket_closed",
            "linked_actor": "orchestrator",
        }
        _append_record(data, actor_record, "zero_ticket_bonus")
    return {"orchestrator": orchestrator_entry, "last_worker": actor_entry}


def get_scores() -> dict:
    """Return all worker scores."""
    return _load()["scores"]


def get_leaderboard() -> list:
    """Return workers sorted by total score (descending)."""
    scores = get_scores()
    return sorted(
        [{"worker": w, **s} for w, s in scores.items()],
        key=lambda x: x["total"],
        reverse=True,
    )


def get_history(worker: str | None = None) -> list:
    """Return scoring history, optionally filtered by worker."""
    history = _load()["history"]
    if worker:
        return [h for h in history if h["worker"] == worker]
    return history


def adjust_score(worker_name: str, delta: float, reason: str,
                 adjuster: str = "system") -> dict:
    """Convenience wrapper: adjust a worker's score by arbitrary delta.

    Positive delta = award, negative delta = deduction.
    For negative deltas, dispatch evidence is required unless adjuster
    is 'spam_guard' (auto-forced because spam is detected at publish time).
    """
    if delta >= 0:
        return award_points(worker_name, reason, adjuster, amount=abs(delta))
    else:
        # spam_guard penalties auto-force (no dispatch evidence needed)
        force = (adjuster == "spam_guard")
        return deduct_points(worker_name, reason, adjuster,
                             amount=abs(delta), force=force)
    # signed: alpha


def get_score(worker_name: str):
    """Return a single worker's score dict, or None if not found."""
    scores = get_scores()
    return scores.get(worker_name)
    # signed: alpha


def main():
    parser = argparse.ArgumentParser(
        description="Skynet Worker Scoring System"
    )
    parser.add_argument(
        "--award", metavar="WORKER", help="Award points to a worker"
    )
    parser.add_argument(
        "--deduct", metavar="WORKER", help="Deduct points from a worker"
    )
    parser.add_argument(
        "--file-bug", metavar="WORKER",
        help="Reward a worker for filing a bug for cross-validation",
    )
    parser.add_argument(
        "--confirm-bug", metavar="WORKER",
        help="Reward both the original filer and cross-validator when a bug is confirmed",
    )
    parser.add_argument(
        "--refactor", metavar="WORKER",
        help="Apply baseline refactor deduction to a worker",
    )
    parser.add_argument(
        "--refactor-necessary", metavar="WORKER",
        help="Reverse refactor deduction after an unbiased validator confirms necessity",
    )
    parser.add_argument(
        "--biased-refactor-report", metavar="WORKER",
        help="Apply biased self-report penalty for a refactor",
    )
    parser.add_argument(
        "--proactive-ticket-clear", metavar="ACTOR",
        help="Reward orchestrator/consultant proactive ticket clearing",
    )
    parser.add_argument(
        "--autonomous-pull", metavar="WORKER",
        help="Reward a worker for autonomously pulling the next ticket",
    )
    parser.add_argument(
        "--zero-ticket-bonus",
        action="store_true",
        help="Award orchestrator and final ticket closer when queue reaches zero",
    )
    parser.add_argument(
        "--task-id", metavar="TID", help="Task identifier (for award/deduct)"
    )
    parser.add_argument(
        "--validator",
        metavar="VNAME",
        help="Validating worker name (for award/deduct)",
    )
    parser.add_argument(
        "--amount",
        type=float,
        default=None,
        help="Custom point amount (default: 0.01 award, 0.005 deduct)",
    )
    parser.add_argument(
        "--last-worker",
        metavar="WORKER",
        help="Actor that closed the final ticket for --zero-ticket-bonus",
    )
    parser.add_argument(
        "--leaderboard",
        action="store_true",
        help="Show worker leaderboard",
    )
    parser.add_argument(
        "--history",
        nargs="?",
        const="__all__",
        metavar="WORKER",
        help="Show scoring history (optionally for a specific worker)",
    )
    parser.add_argument(
        "--score", metavar="WORKER", help="Show score for a specific worker"
    )

    args = parser.parse_args()

    if args.award:
        if not args.task_id or not args.validator:
            parser.error("--award requires --task-id and --validator")
        amt = args.amount if args.amount is not None else DEFAULT_AWARD
        result = award_points(args.award, args.task_id, args.validator, amt)
        print(f"Awarded {amt} pts to {args.award} (total: {result['total']})")

    elif args.deduct:
        if not args.task_id or not args.validator:
            parser.error("--deduct requires --task-id and --validator")
        amt = args.amount if args.amount is not None else DEFAULT_DEDUCT
        result = deduct_points(args.deduct, args.task_id, args.validator, amt)
        print(f"Deducted {amt} pts from {args.deduct} (total: {result['total']})")

    elif args.file_bug:
        if not args.task_id:
            parser.error("--file-bug requires --task-id")
        amt = args.amount if args.amount is not None else None
        validator = args.validator or "orchestrator"
        result = award_bug_report(args.file_bug, args.task_id, validator, amt)
        used_amt = amt if amt is not None else _load_protocol()["bug_report_award"]
        print(
            f"Awarded bug filing {used_amt} pts to "
            f"{args.file_bug} (total: {result['total']})"
        )

    elif args.confirm_bug:
        if not args.task_id or not args.validator:
            parser.error("--confirm-bug requires --task-id and --validator")
        amt = args.amount if args.amount is not None else None
        result = confirm_bug_report(args.confirm_bug, args.task_id, args.validator, amt)
        used_amt = (
            amt if amt is not None else _load_protocol()["bug_report_confirmation_award"]
        )
        print(
            f"Confirmed bug for {args.confirm_bug}: reporter +{used_amt} pts "
            f"(total: {result['reporter']['total']}); validator {args.validator} "
            f"+{used_amt} pts (total: {result['validator']['total']})"
        )

    elif args.refactor:
        if not args.task_id:
            parser.error("--refactor requires --task-id")
        amt = args.amount if args.amount is not None else None
        source = args.validator or "orchestrator"
        result = deduct_for_refactor(args.refactor, args.task_id, source, amt)
        used_amt = amt if amt is not None else _load_protocol()["refactor_deduction"]
        print(
            f"Applied refactor deduction {used_amt} pts to {args.refactor} "
            f"(total: {result['total']})"
        )

    elif args.refactor_necessary:
        if not args.task_id or not args.validator:
            parser.error("--refactor-necessary requires --task-id and --validator")
        amt = args.amount if args.amount is not None else None
        result = cancel_refactor_deduction_if_necessary(
            args.refactor_necessary, args.task_id, args.validator, amt
        )
        used_amt = (
            amt if amt is not None else _load_protocol()["refactor_necessary_reversal"]
        )
        print(
            f"Reversed refactor deduction by {used_amt} pts for "
            f"{args.refactor_necessary} (total: {result['total']})"
        )

    elif args.biased_refactor_report:
        if not args.task_id or not args.validator:
            parser.error("--biased-refactor-report requires --task-id and --validator")
        amt = args.amount if args.amount is not None else None
        result = deduct_for_biased_refactor_report(
            args.biased_refactor_report, args.task_id, args.validator, amt
        )
        used_amt = (
            amt if amt is not None else _load_protocol()["biased_refactor_report_deduction"]
        )
        print(
            f"Applied biased refactor report deduction {used_amt} pts to "
            f"{args.biased_refactor_report} (total: {result['total']})"
        )

    elif args.proactive_ticket_clear:
        if not args.task_id:
            parser.error("--proactive-ticket-clear requires --task-id")
        amt = args.amount if args.amount is not None else None
        validator = args.validator or "god"
        result = award_proactive_ticket_clear(
            args.proactive_ticket_clear, args.task_id, validator, amt
        )
        used_amt = (
            amt if amt is not None else _load_protocol()["proactive_ticket_clear_award"]
        )
        print(
            f"Awarded proactive ticket clear {used_amt} pts to "
            f"{args.proactive_ticket_clear} (total: {result['total']})"
        )

    elif args.autonomous_pull:
        if not args.task_id:
            parser.error("--autonomous-pull requires --task-id")
        amt = args.amount if args.amount is not None else None
        validator = args.validator or "orchestrator"
        result = award_autonomous_pull(
            args.autonomous_pull, args.task_id, validator, amt
        )
        used_amt = (
            amt if amt is not None else _load_protocol()["autonomous_pull_award"]
        )
        print(
            f"Awarded autonomous pull {used_amt} pts to "
            f"{args.autonomous_pull} (total: {result['total']})"
        )

    elif args.zero_ticket_bonus:
        if not args.task_id or not args.last_worker:
            parser.error("--zero-ticket-bonus requires --task-id and --last-worker")
        amt = args.amount if args.amount is not None else None
        validator = args.validator or "god"
        result = award_zero_ticket_clear(args.task_id, args.last_worker, validator, amt)
        used_amt = (
            amt if amt is not None else _load_protocol()["ticket_zero_bonus_award"]
        )
        print(
            f"Awarded zero-ticket bonus {used_amt} pts to orchestrator "
            f"(total: {result['orchestrator']['total']}) and {args.last_worker} "
            f"(total: {result['last_worker']['total']})"
        )

    elif args.leaderboard:
        board = get_leaderboard()
        if not board:
            print("No scores recorded yet.")
            return
        print(
            f"{'Rank':<5} {'Worker':<20} {'Score':<10} {'Awards':<8} "
            f"{'Deductions':<10} {'RefD':<6} {'RefOK':<6} {'Bias':<6} "
            f"{'PClr':<6} {'Auto':<6} {'BugR':<6} {'BugOK':<6} "
            f"{'BugX':<6} {'Zero':<6}"
        )
        print("-" * 136)
        for i, entry in enumerate(board, 1):
            print(
                f"{i:<5} {entry['worker']:<20} {entry['total']:<10.4f} "
                f"{entry['awards']:<8} {entry['deductions']:<10} "
                f"{entry.get('refactor_deductions', 0):<6} "
                f"{entry.get('refactor_reversals', 0):<6} "
                f"{entry.get('bias_penalties', 0):<6} "
                f"{entry.get('proactive_ticket_clears', 0):<6} "
                f"{entry.get('autonomous_pull_awards', 0):<6} "
                f"{entry.get('bug_reports_filed', 0):<6} "
                f"{entry.get('bug_report_confirmations', 0):<6} "
                f"{entry.get('bug_cross_validations', 0):<6} "
                f"{entry.get('zero_ticket_bonus_awards', 0):<6}"
            )

    elif args.history is not None:
        worker = None if args.history == "__all__" else args.history
        records = get_history(worker)
        if not records:
            print(f"No history{' for ' + worker if worker else ''}.")
            return
        for r in records[-20:]:
            sign = "+" if r["action"] in (
                "award",
                "bug_report_filed",
                "bug_report_confirmed",
                "bug_cross_validation_award",
                "refactor_reversal",
                "proactive_ticket_clear",
                "autonomous_pull_award",
                "zero_ticket_bonus",
            ) else "-"
            print(
                f"  {r['timestamp'][:19]}  {sign}{r['amount']:.3f}  "
                f"{r['worker']:<8} validated_by={r['validator']:<8} "
                f"task={r['task_id']}  total={r['new_total']:.4f}"
            )

    elif args.score:
        scores = get_scores()
        if args.score not in scores:
            print(f"No score recorded for {args.score}.")
            return
        s = scores[args.score]
        print(
            f"{args.score}: total={s['total']:.4f}  "
            f"awards={s['awards']}  deductions={s['deductions']}  "
            f"refactor_deductions={s.get('refactor_deductions', 0)}  "
            f"refactor_reversals={s.get('refactor_reversals', 0)}  "
            f"bias_penalties={s.get('bias_penalties', 0)}  "
            f"proactive_ticket_clears={s.get('proactive_ticket_clears', 0)}  "
            f"autonomous_pull_awards={s.get('autonomous_pull_awards', 0)}  "
            f"bug_reports_filed={s.get('bug_reports_filed', 0)}  "
            f"bug_report_confirmations={s.get('bug_report_confirmations', 0)}  "
            f"bug_cross_validations={s.get('bug_cross_validations', 0)}  "
            f"zero_ticket_bonus_awards={s.get('zero_ticket_bonus_awards', 0)}"
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
