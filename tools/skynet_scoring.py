"""Skynet Worker Scoring System.

Tracks cross-validated task completions, bug validation, ticket awareness,
zero-ticket completion bonuses, refactor-review penalties, workspace
cleanliness accountability (Rule 0.9), and consultant advisory contributions
(Convention 3 fairness fixes).

Workers earn points when their work or findings are validated by a
*different* actor.

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
                                  (300s cooldown per agent)
  - advisory contribution:        +0.05 pts when a consultant's advisory
                                  proposal is accepted and acted upon
  - cross-system review:          +0.02 pts when a consultant performs a
                                  cross-system review with actionable findings

Rule 0.9 -- Workspace Cleanliness & Tool Usage Accountability:
  - uncleared work:               -0.02 orch / -0.01 workers/consultants
  - tool bypass:                  -0.02 orch / -0.01 workers/consultants
  - repeat offense:               -0.50 all (previously addressed, repeated)
  - cleanup help:                 +0.01 for helping clean the workspace
  - cleanup cross-validate:       +0.01 for cross-validating cleanup
  - invalid cleanup finder:       +0.02 for catching false/invalid cleanup

Convention 3 Fairness Fixes:
  - Consultants receive base=6.0 on first creation (same as workers)
  - Zero-ticket bonus has 300s cooldown per agent to prevent inflation
  - reset_illegitimate_deductions() reverses forced boot-window spam penalties

Convention 4 Fairness Enhancements (coding_4, signed: beta):
  - dispatch_evidence_for_all_deductions flag in brain_config.json
  - All deduction functions check dispatch evidence when flag is True
  - reset_illegitimate_deductions() now handles all deduction types, not just spam
  - System-level deductions (spam_guard, process violations) always bypass evidence check

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
  python tools/skynet_scoring.py --advisory-contribution AGENT --task-id TID --validator VNAME
  python tools/skynet_scoring.py --cross-system-review AGENT --task-id TID --validator VNAME
  python tools/skynet_scoring.py --reset-illegitimate AGENT
  python tools/skynet_scoring.py --uncleared-work AGENT --task-id TID --validator VNAME
  python tools/skynet_scoring.py --tool-bypass AGENT --task-id TID --validator VNAME
  python tools/skynet_scoring.py --repeat-offense AGENT --task-id TID --validator VNAME
  python tools/skynet_scoring.py --cleanup-help AGENT --task-id TID --validator VNAME
  python tools/skynet_scoring.py --cleanup-cv AGENT --task-id TID --validator VNAME
  python tools/skynet_scoring.py --invalid-cleanup AGENT --task-id TID --validator VNAME
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
BRAIN_CONFIG_FILE = DATA_DIR / "brain_config.json"  # signed: alpha (removed duplicate _lock)
BUS_URL = "http://localhost:8420/bus/publish"
SCHEMA_VERSION = 5

# System/daemon senders — NOT real workers or agents. Their scores come from
# SpamGuard auto-penalties, not task work. Display separately from workers.  # signed: delta
SYSTEM_SENDERS = frozenset({
    "monitor", "convene", "convene-gate", "convene_gate", "self_prompt",
    "system", "overseer", "watchdog", "bus_relay", "learner",
    "self_improve", "sse_daemon", "idle_monitor",
    "skynet_self", "skynet_monitor", "skynet_learner", "skynet_watchdog",
    "skynet_overseer", "skynet_bus_relay", "skynet_self_prompt",
    "skynet_self_improve", "skynet_sse_daemon", "skynet_idle_monitor",
    "bus_watcher", "ws_monitor", "bus_persist", "consultant_consumer",
    "health_report", "worker_loop", "daemon_status",
})  # signed: orchestrator

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
DEFAULT_ADVISORY_CONTRIBUTION_AWARD = 0.05  # signed: gamma
DEFAULT_CROSS_SYSTEM_REVIEW_AWARD = 0.02  # signed: gamma
ZTB_COOLDOWN_SECONDS = 300  # Min seconds between zero-ticket bonuses per agent  # signed: gamma

# Rule 0.9: Workspace Cleanliness & Tool Usage Accountability
DEFAULT_UNCLEARED_WORK_DEDUCT_ORCH = 0.02
DEFAULT_UNCLEARED_WORK_DEDUCT_OTHER = 0.01
DEFAULT_TOOL_BYPASS_DEDUCT_ORCH = 0.02
DEFAULT_TOOL_BYPASS_DEDUCT_OTHER = 0.01
DEFAULT_REPEAT_OFFENSE_DEDUCT = 0.5
DEFAULT_CLEANUP_HELP_AWARD = 0.01
DEFAULT_CLEANUP_CV_AWARD = 0.01
DEFAULT_INVALID_CLEANUP_AWARD = 0.02

ORCHESTRATOR_ROLES = frozenset({"orchestrator"})
CONSULTANT_ROLES = frozenset({"consultant", "gemini_consultant"})

WORKER_ROLES = frozenset({"alpha", "beta", "gamma", "delta"})  # signed: gamma
BASE_SCORE_AGENTS = WORKER_ROLES | CONSULTANT_ROLES | ORCHESTRATOR_ROLES  # signed: gamma

# Adjusters that bypass dispatch evidence checks (system-level penalties).  # signed: beta
SYSTEM_ADJUSTERS = frozenset({"spam_guard", "process_violation", "system"})

_lock = threading.Lock()  # Guards all read-modify-write cycles on SCORES_FILE  # signed: gamma
# Note: single lock instance for thread safety (duplicate at line 72 removed) # signed: alpha


def _empty_store() -> dict:
    return {"version": SCHEMA_VERSION, "scores": {}, "history": []}


def reset_scores(reason: str = "manual_reset") -> dict:
    """Reset ALL scores to zero with a clean slate. Backs up old scores first.

    Returns the new empty score store.
    """
    with _lock:
        old = _load()
        # Backup current scores — ensure DATA_DIR exists first
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = DATA_DIR / f"worker_scores_backup_{ts}.json"
        backup_path.write_text(
            json.dumps(old, indent=2, default=str), encoding="utf-8"
        )
        # Create fresh store with reset event in history
        new_store = _empty_store()
        new_store["history"].append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "worker": "ALL",
            "action": "reset",
            "amount": 0,
            "task_id": reason,
            "validator": "orchestrator",
            "new_total": 0.0,
        })
        _save(new_store)
        print(f"Scores RESET. Backup saved to {backup_path.name}")
        return new_store


def amnesty(targets: str = "all", categories: str = "spam_guard") -> dict:
    """Reverse unfair spam_guard deductions for specified workers.

    Args:
        targets: Worker name, "all" for all workers, or "system" for system senders only.
        categories: Comma-separated validator names to amnesty (default: "spam_guard").

    Returns:
        Dict of {worker: reversed_amount} for each amnestied worker.
    """
    cat_set = set(c.strip() for c in categories.split(","))
    with _lock:
        data = _load()
        reversed_map = {}
        history = data.get("history", [])

        # Find all forced spam deductions in history
        for entry in history:
            if entry.get("action") != "deduct":
                continue
            if entry.get("validator", "") not in cat_set:
                continue
            worker = entry.get("worker", "")
            if not worker:
                continue
            if targets == "system" and worker not in SYSTEM_SENDERS:
                continue
            if targets not in ("all", "system") and worker != targets:
                continue
            # Use abs() to handle both positive deduction amounts and negative stored values
            amt = entry.get("amount", None)
            if amt is None:
                continue  # Skip malformed history entries silently would hide data
            reversed_map[worker] = reversed_map.get(worker, 0) + abs(float(amt))

        # Apply reversals
        for worker, amount in reversed_map.items():
            _ensure_worker(data, worker)
            ws = data["scores"][worker]
            ws["total"] = ws.get("total", 0) + amount
            ws["awards"] = ws.get("awards", 0) + 1
            data["history"].append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "worker": worker,
                "action": "amnesty",
                "amount": amount,
                "task_id": f"amnesty_{categories}",
                "validator": "orchestrator",
                "new_total": ws["total"],
            })

        _save(data)
        for worker, amount in sorted(reversed_map.items()):
            print(f"  {worker}: reversed {amount:.3f} pts")
        total_reversed = sum(reversed_map.values())
        print(f"Amnesty complete: {len(reversed_map)} workers, {total_reversed:.3f} pts reversed")
        return reversed_map


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
        "dispatch_evidence_for_all_deductions": True,  # signed: beta
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
        if key in scoring and scoring[key] is not None:  # signed: alpha — guard null values
            protocol[key] = float(scoring[key])
    if "require_independent_refactor_validation" in scoring:
        protocol["require_independent_refactor_validation"] = bool(
            scoring["require_independent_refactor_validation"]
        )
    if "dispatch_evidence_for_all_deductions" in scoring:  # signed: beta
        protocol["dispatch_evidence_for_all_deductions"] = bool(
            scoring["dispatch_evidence_for_all_deductions"]
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
    entry.setdefault("uncleared_work_deductions", 0)
    entry.setdefault("tool_bypass_deductions", 0)
    entry.setdefault("repeat_offense_deductions", 0)
    entry.setdefault("cleanup_help_awards", 0)
    entry.setdefault("cleanup_cv_awards", 0)
    entry.setdefault("invalid_cleanup_awards", 0)
    entry.setdefault("advisory_contribution_awards", 0)  # signed: gamma
    entry.setdefault("cross_system_review_awards", 0)  # signed: gamma
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
    """Initialize a worker entry if it doesn't exist.

    Grants base=6.0 to all real agents (workers, orchestrator, consultants)
    on first creation to ensure equal starting scores per Rule 0.6.
    """  # signed: gamma
    if worker not in data["scores"]:
        entry = _normalize_worker_entry({})
        if worker in BASE_SCORE_AGENTS:
            entry.setdefault("base", 6.0)
            entry["total"] = entry.get("total", 0.0) + 6.0
        data["scores"][worker] = entry
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


def _ztb_cooldown_ok(data: dict, agent: str) -> bool:
    """Check if enough time has passed since the last ZTB for this agent.

    Enforces ZTB_COOLDOWN_SECONDS (300s) between consecutive zero-ticket
    bonuses per agent to prevent rapid-fire inflation.
    """  # signed: gamma
    now = datetime.now(timezone.utc)
    for record in reversed(data.get("history", [])):
        if (record.get("worker") == agent
                and record.get("action") == "zero_ticket_bonus"):
            try:
                ts_str = record["timestamp"]
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                elapsed = (now - ts).total_seconds()
                return elapsed >= ZTB_COOLDOWN_SECONDS
            except (KeyError, ValueError):
                return True  # Malformed timestamp, allow
    return True  # No prior ZTB for this agent


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


def _guard_dispatch_evidence(
    worker: str, task_id: str, amount: float, action: str,
    validator: str = "",
) -> bool:
    """Check dispatch evidence if the protocol requires it for all deductions.

    Returns True if the deduction should proceed, False if it should be
    rejected.  System-level validators (spam_guard, process_violation)
    always bypass the check.
    """  # signed: beta
    if validator in SYSTEM_ADJUSTERS:
        return True  # system penalties always apply
    protocol = _load_protocol()
    if not protocol.get("dispatch_evidence_for_all_deductions", True):
        return True  # flag disabled -- allow all deductions
    evidence = verify_dispatch_evidence(worker, task_id)
    if not evidence["verified"]:
        import sys
        print(
            f"[scoring] REJECTED {action} deduction of {amount} from {worker}: "
            f"dispatch evidence not verified. "
            f"found={evidence['dispatch_found']}, "
            f"success={evidence['dispatch_success']}, "
            f"result_received={evidence['result_received']}",
            file=sys.stderr,
        )
        return False
    return True
    # signed: beta


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

    # Dispatch evidence guard  # signed: beta
    if not _guard_dispatch_evidence(worker, task_id, deduction,
                                    "refactor_deduct", validator):
        return None

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

    # Dispatch evidence guard  # signed: beta
    if not _guard_dispatch_evidence(worker, task_id, penalty,
                                    "biased_refactor_report", validator):
        return None

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

        # Cooldown check: prevent rapid-fire ZTB inflation  # signed: gamma
        if not _ztb_cooldown_ok(data, "orchestrator"):
            raise ValueError(
                f"Zero-ticket bonus cooldown: orchestrator received ZTB "
                f"within last {ZTB_COOLDOWN_SECONDS}s"
            )
        if not _ztb_cooldown_ok(data, actor):
            raise ValueError(
                f"Zero-ticket bonus cooldown: {actor} received ZTB "
                f"within last {ZTB_COOLDOWN_SECONDS}s"
            )

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


# ── Convention 3 Fix: Consultant Earning Mechanisms ──────────────────────

def award_advisory_contribution(
    agent: str,
    task_id: str,
    validator: str,
    amount: float | None = None,
) -> dict:
    """Award +0.05 to a consultant for an advisory contribution acted upon.

    Consultants (Codex, Gemini) earn points when their proposals, reviews,
    or advisory insights are accepted and acted upon by the orchestrator
    or a worker. This addresses Convention 3 Bias #2 (no earning mechanism
    for consultants).

    Args:
        agent: The consultant being rewarded.
        task_id: Identifier of the advisory contribution.
        validator: The agent who validated the contribution was useful.
        amount: Points to award (default 0.05).

    Returns:
        Updated score entry for the consultant.
    """  # signed: gamma
    amt = amount if amount is not None else DEFAULT_ADVISORY_CONTRIBUTION_AWARD
    _require_independent_validator(agent, validator, "validate advisory contribution for")

    with _lock:
        data = _load()
        _ensure_worker(data, agent)
        entry = data["scores"][agent]
        entry["total"] = round(entry["total"] + amt, 6)
        entry["awards"] += 1
        entry["advisory_contribution_awards"] = (
            entry.get("advisory_contribution_awards", 0) + 1
        )
        record = {
            "worker": agent,
            "action": "advisory_contribution",
            "amount": amt,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": entry["total"],
            "protocol": "convention_3_fairness",
            "finding": "consultant_advisory_accepted",
        }
        _append_record(data, record, "advisory_contribution")
    return entry


def award_cross_system_review(
    agent: str,
    task_id: str,
    validator: str,
    amount: float | None = None,
) -> dict:
    """Award +0.02 to a consultant for a cross-system code review.

    Consultants earn points when they perform code reviews, architecture
    assessments, or cross-system validations that produce actionable
    findings. This addresses Convention 3 Bias #2.

    Args:
        agent: The consultant being rewarded.
        task_id: Identifier of the review task.
        validator: The agent who confirmed the review was valuable.
        amount: Points to award (default 0.02).

    Returns:
        Updated score entry for the consultant.
    """  # signed: gamma
    amt = amount if amount is not None else DEFAULT_CROSS_SYSTEM_REVIEW_AWARD
    _require_independent_validator(agent, validator, "validate cross-system review for")

    with _lock:
        data = _load()
        _ensure_worker(data, agent)
        entry = data["scores"][agent]
        entry["total"] = round(entry["total"] + amt, 6)
        entry["awards"] += 1
        entry["cross_system_review_awards"] = (
            entry.get("cross_system_review_awards", 0) + 1
        )
        record = {
            "worker": agent,
            "action": "cross_system_review",
            "amount": amt,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": entry["total"],
            "protocol": "convention_3_fairness",
            "finding": "cross_system_review_validated",
        }
        _append_record(data, record, "cross_system_review")
    return entry


# ── Convention 3 Fix: Reset Illegitimate Deductions ──────────────────────

def reset_illegitimate_deductions(
    agent: str,
    boot_window_seconds: int = 300,
) -> dict:
    """Remove forced/unverified deductions that lack dispatch evidence.

    Two passes:
    1. Forced spam_guard deductions within the boot window (original behavior)
    2. Any deduction that lacks dispatch evidence when the flag is enabled

    Previously only reversed spam_guard forced deductions within boot window.
    Enhanced by coding_4 to check ALL deductions against dispatch evidence.

    Args:
        agent: Agent whose illegitimate deductions should be reversed.
        boot_window_seconds: Seconds after first activity to consider
            as boot window (default 300 = 5 minutes).

    Returns:
        Dict with keys: agent, reversed_count, reversed_amount, new_total,
        pass1_count, pass2_count.
    """  # signed: beta
    with _lock:
        data = _load()
        _ensure_worker(data, agent)
        history = data.get("history", [])

        # Find earliest activity timestamp for this agent
        agent_entries = [
            h for h in history if h.get("worker") == agent
        ]
        if not agent_entries:
            return {"agent": agent, "reversed_count": 0,
                    "reversed_amount": 0.0, "new_total": data["scores"][agent]["total"],
                    "pass1_count": 0, "pass2_count": 0}

        try:
            first_ts = min(
                datetime.fromisoformat(h["timestamp"])
                for h in agent_entries if "timestamp" in h
            )
            if first_ts.tzinfo is None:
                first_ts = first_ts.replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            return {"agent": agent, "reversed_count": 0,
                    "reversed_amount": 0.0, "new_total": data["scores"][agent]["total"],
                    "pass1_count": 0, "pass2_count": 0}

        reversed_amount = 0.0
        reversed_count = 0
        pass1_count = 0
        pass2_count = 0

        # Deduction action types that should be checked  # signed: beta
        deduction_actions = frozenset({
            "deduct", "refactor_deduct", "biased_refactor_report",
            "uncleared_work", "tool_bypass", "repeat_offense",
        })

        for entry in history:
            if entry.get("worker") != agent:
                continue
            if entry.get("action") not in deduction_actions:
                continue
            if entry.get("_reversed_by_convention3") or entry.get("_reversed_by_coding4"):
                continue  # already reversed

            is_forced_spam = (
                entry.get("action") == "deduct"
                and entry.get("forced")
                and entry.get("validator") == "spam_guard"
            )

            # Pass 1: forced spam deductions within boot window
            if is_forced_spam:
                try:
                    entry_ts = datetime.fromisoformat(entry["timestamp"])
                    if entry_ts.tzinfo is None:
                        entry_ts = entry_ts.replace(tzinfo=timezone.utc)
                except (ValueError, KeyError):
                    continue
                elapsed = (entry_ts - first_ts).total_seconds()
                if elapsed <= boot_window_seconds:
                    amt = abs(float(entry.get("amount", 0)))
                    reversed_amount += amt
                    reversed_count += 1
                    pass1_count += 1
                    entry["_reversed_by_convention3"] = True
                continue

            # Pass 2: any non-system deduction without dispatch evidence
            validator = entry.get("validator", "")
            if validator in SYSTEM_ADJUSTERS:
                continue  # system penalties are always legitimate
            task_id = entry.get("task_id", "")
            evidence = verify_dispatch_evidence(agent, task_id)
            if not evidence["dispatch_found"]:
                # No dispatch record at all -- deduction is illegitimate
                amt = abs(float(entry.get("amount", 0)))
                reversed_amount += amt
                reversed_count += 1
                pass2_count += 1
                entry["_reversed_by_coding4"] = True

        # Apply reversal
        if reversed_amount > 0:
            ws = data["scores"][agent]
            ws["total"] = round(ws["total"] + reversed_amount, 6)
            ws["awards"] += 1
            data["history"].append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "worker": agent,
                "action": "illegitimate_deduction_reversal",
                "amount": reversed_amount,
                "task_id": f"coding4_fairness_reset_{agent}",
                "validator": "coding4_fairness_audit",
                "new_total": ws["total"],
                "protocol": "coding_4_fairness",
                "finding": (
                    f"reversed {reversed_count} illegitimate deductions "
                    f"(pass1_boot_spam={pass1_count}, "
                    f"pass2_no_dispatch={pass2_count})"
                ),
                "reversed_count": reversed_count,
            })
            _save(data)

        entry_final = data["scores"][agent]
        return {
            "agent": agent,
            "reversed_count": reversed_count,
            "reversed_amount": round(reversed_amount, 6),
            "new_total": entry_final["total"],
            "pass1_count": pass1_count,
            "pass2_count": pass2_count,
        }
    # signed: beta


# ── Rule 0.9: Workspace Cleanliness & Tool Usage Accountability ──────────

def _cleanliness_deduct_amount(agent: str) -> float:
    """Return the appropriate deduction amount based on agent role."""
    if agent in ORCHESTRATOR_ROLES:
        return DEFAULT_UNCLEARED_WORK_DEDUCT_ORCH
    return DEFAULT_UNCLEARED_WORK_DEDUCT_OTHER


def deduct_uncleared_work(
    agent: str,
    task_id: str,
    validator: str,
    amount: float | None = None,
) -> dict:
    """Deduct points for leaving uncleared tasks/todos/incidents in the system.

    Orchestrator: -0.02, Workers/Consultants: -0.01.
    """
    _require_independent_validator(agent, validator, "audit uncleared work for")
    amt = amount if amount is not None else _cleanliness_deduct_amount(agent)

    # Dispatch evidence guard  # signed: beta
    if not _guard_dispatch_evidence(agent, task_id, amt,
                                    "uncleared_work", validator):
        return None

    with _lock:
        data = _load()
        _ensure_worker(data, agent)
        entry = data["scores"][agent]
        entry["total"] = round(entry["total"] - amt, 6)
        entry["deductions"] += 1
        entry["uncleared_work_deductions"] = entry.get("uncleared_work_deductions", 0) + 1
        record = {
            "worker": agent,
            "action": "uncleared_work",
            "amount": amt,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": entry["total"],
            "protocol": "rule_0_9_cleanliness",
        }
        _append_record(data, record, "uncleared_work")
    return entry


def deduct_tool_bypass(
    agent: str,
    task_id: str,
    validator: str,
    amount: float | None = None,
) -> dict:
    """Deduct points for not using Skynet intelligence tools when available.

    Orchestrator: -0.02, Workers/Consultants: -0.01.
    """
    _require_independent_validator(agent, validator, "audit tool bypass for")
    amt = amount if amount is not None else _cleanliness_deduct_amount(agent)

    # Dispatch evidence guard  # signed: beta
    if not _guard_dispatch_evidence(agent, task_id, amt,
                                    "tool_bypass", validator):
        return None

    with _lock:
        data = _load()
        _ensure_worker(data, agent)
        entry = data["scores"][agent]
        entry["total"] = round(entry["total"] - amt, 6)
        entry["deductions"] += 1
        entry["tool_bypass_deductions"] = entry.get("tool_bypass_deductions", 0) + 1
        record = {
            "worker": agent,
            "action": "tool_bypass",
            "amount": amt,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": entry["total"],
            "protocol": "rule_0_9_cleanliness",
        }
        _append_record(data, record, "tool_bypass")
    return entry


def deduct_repeat_offense(
    agent: str,
    task_id: str,
    validator: str,
    amount: float | None = None,
) -> dict:
    """Deduct -0.50 for repeating an offense that was already addressed.

    Applied universally to orchestrator, workers, and consultants.
    """
    _require_independent_validator(agent, validator, "audit repeat offense for")
    amt = amount if amount is not None else DEFAULT_REPEAT_OFFENSE_DEDUCT

    # Dispatch evidence guard  # signed: beta
    if not _guard_dispatch_evidence(agent, task_id, amt,
                                    "repeat_offense", validator):
        return None

    with _lock:
        data = _load()
        _ensure_worker(data, agent)
        entry = data["scores"][agent]
        entry["total"] = round(entry["total"] - amt, 6)
        entry["deductions"] += 1
        entry["repeat_offense_deductions"] = entry.get("repeat_offense_deductions", 0) + 1
        record = {
            "worker": agent,
            "action": "repeat_offense",
            "amount": amt,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": entry["total"],
            "protocol": "rule_0_9_cleanliness",
            "finding": "previously_addressed_offense_repeated",
        }
        _append_record(data, record, "repeat_offense")
    return entry


def award_cleanup_help(
    agent: str,
    task_id: str,
    validator: str,
    amount: float | None = None,
) -> dict:
    """Award +0.01 for helping make the workspace clean."""
    _require_independent_validator(agent, validator, "validate cleanup for")
    amt = amount if amount is not None else DEFAULT_CLEANUP_HELP_AWARD
    with _lock:
        data = _load()
        _ensure_worker(data, agent)
        entry = data["scores"][agent]
        entry["total"] = round(entry["total"] + amt, 6)
        entry["awards"] += 1
        entry["cleanup_help_awards"] = entry.get("cleanup_help_awards", 0) + 1
        record = {
            "worker": agent,
            "action": "cleanup_help",
            "amount": amt,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": entry["total"],
            "protocol": "rule_0_9_cleanliness",
        }
        _append_record(data, record, "cleanup_help")
    return entry


def award_cleanup_cross_validate(
    agent: str,
    task_id: str,
    validator: str,
    amount: float | None = None,
) -> dict:
    """Award +0.01 for cross-validating cleanup work."""
    _require_independent_validator(agent, validator, "record cleanup cv for")
    amt = amount if amount is not None else DEFAULT_CLEANUP_CV_AWARD
    with _lock:
        data = _load()
        _ensure_worker(data, agent)
        entry = data["scores"][agent]
        entry["total"] = round(entry["total"] + amt, 6)
        entry["awards"] += 1
        entry["cleanup_cv_awards"] = entry.get("cleanup_cv_awards", 0) + 1
        record = {
            "worker": agent,
            "action": "cleanup_cross_validate",
            "amount": amt,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": entry["total"],
            "protocol": "rule_0_9_cleanliness",
        }
        _append_record(data, record, "cleanup_cross_validate")
    return entry


def award_invalid_cleanup_finder(
    agent: str,
    task_id: str,
    validator: str,
    amount: float | None = None,
) -> dict:
    """Award +0.02 for finding invalid/false cleanup."""
    _require_independent_validator(agent, validator, "validate invalid cleanup finding for")
    amt = amount if amount is not None else DEFAULT_INVALID_CLEANUP_AWARD
    with _lock:
        data = _load()
        _ensure_worker(data, agent)
        entry = data["scores"][agent]
        entry["total"] = round(entry["total"] + amt, 6)
        entry["awards"] += 1
        entry["invalid_cleanup_awards"] = entry.get("invalid_cleanup_awards", 0) + 1
        record = {
            "worker": agent,
            "action": "invalid_cleanup_found",
            "amount": amt,
            "task_id": task_id,
            "validator": validator,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "new_total": entry["total"],
            "protocol": "rule_0_9_cleanliness",
            "finding": "false_cleanup_detected",
        }
        _append_record(data, record, "invalid_cleanup_found")
    return entry


def get_scores() -> dict:
    """Return all worker scores."""
    return _load()["scores"]


def get_leaderboard(include_system: bool = True) -> list:
    """Return workers sorted by total score (descending).

    If *include_system* is True (default), all senders are returned.
    Use ``is_system_sender()`` to partition results afterward.
    """
    scores = get_scores()
    entries = [{"worker": w, **s} for w, s in scores.items()]
    if not include_system:
        entries = [e for e in entries if not is_system_sender(e["worker"])]
    return sorted(entries, key=lambda x: x["total"], reverse=True)
    # signed: delta


def is_system_sender(name: str) -> bool:
    """Return True if *name* is a daemon/system sender, not a real worker."""
    return name in SYSTEM_SENDERS
    # signed: delta


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
        "--reset",
        action="store_true",
        help="Reset ALL scores to zero (backs up current scores first)",
    )
    parser.add_argument(
        "--amnesty",
        nargs="?",
        const="all",
        metavar="WORKER|all|system",
        help="Reverse unfair spam_guard deductions (default: all workers)",
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
    # Rule 0.9: Workspace Cleanliness & Tool Usage Accountability
    parser.add_argument(
        "--uncleared-work", metavar="AGENT",
        help="Deduct for leaving uncleared tasks/todos/incidents (-0.02 orch, -0.01 others)",
    )
    parser.add_argument(
        "--tool-bypass", metavar="AGENT",
        help="Deduct for not using Skynet intelligence tools (-0.02 orch, -0.01 others)",
    )
    parser.add_argument(
        "--repeat-offense", metavar="AGENT",
        help="Deduct -0.50 for repeating a previously addressed offense",
    )
    parser.add_argument(
        "--cleanup-help", metavar="AGENT",
        help="Award +0.01 for helping make the workspace clean",
    )
    parser.add_argument(
        "--cleanup-cv", metavar="AGENT",
        help="Award +0.01 for cross-validating cleanup work",
    )
    parser.add_argument(
        "--invalid-cleanup", metavar="AGENT",
        help="Award +0.02 for finding invalid/false cleanup",
    )
    # Convention 3 Fairness: Consultant earning mechanisms  # signed: gamma
    parser.add_argument(
        "--advisory-contribution", metavar="AGENT",
        help="Award +0.05 for a consultant advisory contribution acted upon",
    )
    parser.add_argument(
        "--cross-system-review", metavar="AGENT",
        help="Award +0.02 for a consultant cross-system code review",
    )
    parser.add_argument(
        "--reset-illegitimate", metavar="AGENT",
        help="Reverse forced spam deductions within boot window for an agent",
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

    # ── Rule 0.9 CLI handlers ──
    elif args.uncleared_work:
        if not args.task_id or not args.validator:
            parser.error("--uncleared-work requires --task-id and --validator")
        amt = args.amount if args.amount is not None else None
        result = deduct_uncleared_work(args.uncleared_work, args.task_id, args.validator, amt)
        used = amt if amt is not None else _cleanliness_deduct_amount(args.uncleared_work)
        print(f"Deducted {used} pts from {args.uncleared_work} for uncleared work (total: {result['total']})")

    elif args.tool_bypass:
        if not args.task_id or not args.validator:
            parser.error("--tool-bypass requires --task-id and --validator")
        amt = args.amount if args.amount is not None else None
        result = deduct_tool_bypass(args.tool_bypass, args.task_id, args.validator, amt)
        used = amt if amt is not None else _cleanliness_deduct_amount(args.tool_bypass)
        print(f"Deducted {used} pts from {args.tool_bypass} for tool bypass (total: {result['total']})")

    elif args.repeat_offense:
        if not args.task_id or not args.validator:
            parser.error("--repeat-offense requires --task-id and --validator")
        amt = args.amount if args.amount is not None else DEFAULT_REPEAT_OFFENSE_DEDUCT
        result = deduct_repeat_offense(args.repeat_offense, args.task_id, args.validator, amt)
        print(f"Deducted {amt} pts from {args.repeat_offense} for REPEAT OFFENSE (total: {result['total']})")

    elif args.cleanup_help:
        if not args.task_id or not args.validator:
            parser.error("--cleanup-help requires --task-id and --validator")
        amt = args.amount if args.amount is not None else DEFAULT_CLEANUP_HELP_AWARD
        result = award_cleanup_help(args.cleanup_help, args.task_id, args.validator, amt)
        print(f"Awarded {amt} pts to {args.cleanup_help} for cleanup help (total: {result['total']})")

    elif args.cleanup_cv:
        if not args.task_id or not args.validator:
            parser.error("--cleanup-cv requires --task-id and --validator")
        amt = args.amount if args.amount is not None else DEFAULT_CLEANUP_CV_AWARD
        result = award_cleanup_cross_validate(args.cleanup_cv, args.task_id, args.validator, amt)
        print(f"Awarded {amt} pts to {args.cleanup_cv} for cleanup cross-validation (total: {result['total']})")

    elif args.invalid_cleanup:
        if not args.task_id or not args.validator:
            parser.error("--invalid-cleanup requires --task-id and --validator")
        amt = args.amount if args.amount is not None else DEFAULT_INVALID_CLEANUP_AWARD
        result = award_invalid_cleanup_finder(args.invalid_cleanup, args.task_id, args.validator, amt)
        print(f"Awarded {amt} pts to {args.invalid_cleanup} for finding invalid cleanup (total: {result['total']})")

    # Convention 3 Fairness CLI handlers  # signed: gamma
    elif args.advisory_contribution:
        if not args.task_id or not args.validator:
            parser.error("--advisory-contribution requires --task-id and --validator")
        amt = args.amount if args.amount is not None else DEFAULT_ADVISORY_CONTRIBUTION_AWARD
        result = award_advisory_contribution(args.advisory_contribution, args.task_id, args.validator, amt)
        print(f"Awarded {amt} pts to {args.advisory_contribution} for advisory contribution (total: {result['total']})")

    elif args.cross_system_review:
        if not args.task_id or not args.validator:
            parser.error("--cross-system-review requires --task-id and --validator")
        amt = args.amount if args.amount is not None else DEFAULT_CROSS_SYSTEM_REVIEW_AWARD
        result = award_cross_system_review(args.cross_system_review, args.task_id, args.validator, amt)
        print(f"Awarded {amt} pts to {args.cross_system_review} for cross-system review (total: {result['total']})")

    elif args.reset_illegitimate:
        result = reset_illegitimate_deductions(args.reset_illegitimate)
        print(
            f"Reset illegitimate deductions for {args.reset_illegitimate}: "
            f"reversed {result['reversed_count']} deductions "
            f"({result['reversed_amount']:.3f} pts) -> new total: {result['new_total']:.4f}"
        )

    elif args.reset:
        reset_scores("cli_reset")
        print("All scores have been reset to zero.")

    elif args.amnesty is not None:
        target = args.amnesty
        print(f"Running amnesty for: {target}")
        amnesty(target)

    elif args.leaderboard:
        # Run cleanliness audit summary if available
        try:
            from tools.skynet_cleanliness_audit import run_audit
            audit = run_audit(quiet=True)
            if audit and audit.get("total_issues", 0) > 0:
                print(f"[!] Cleanliness audit: {audit['total_issues']} uncleared items found")
                print()
        except Exception:
            pass
        board = get_leaderboard()
        if not board:
            print("No scores recorded yet.")
            return

        # Separate worker/agent scores from system/daemon scores  # signed: delta
        worker_board = [e for e in board if not is_system_sender(e["worker"])]
        system_board = [e for e in board if is_system_sender(e["worker"])]

        header = (
            f"{'Rank':<5} {'Worker':<20} {'Score':<10} {'Awards':<8} "
            f"{'Deductions':<10} {'RefD':<6} {'RefOK':<6} {'Bias':<6} "
            f"{'PClr':<6} {'Auto':<6} {'BugR':<6} {'BugOK':<6} "
            f"{'BugX':<6} {'Zero':<6}"
        )
        separator = "-" * 136

        def _print_row(rank, entry):
            print(
                f"{rank:<5} {entry['worker']:<20} {entry['total']:<10.4f} "
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

        # -- Workers & Agents --
        print("=== Workers & Agents ===")
        print(header)
        print(separator)
        for i, entry in enumerate(worker_board, 1):
            _print_row(i, entry)

        # -- System / Daemon Senders --
        if system_board:
            print()
            print("=== System / Daemon Senders (spam penalties only) ===")
            print(header)
            print(separator)
            for i, entry in enumerate(system_board, 1):
                _print_row(i, entry)
        # signed: delta

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
