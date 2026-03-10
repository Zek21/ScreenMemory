"""Post-dispatch verification: confirms worker actually received the task.

After each dispatch, waits then checks worker state via UIA. If the worker
is still IDLE (not PROCESSING), the dispatch silently failed -- re-dispatches
once with a warning. All verifications logged to data/dispatch_audit.json.

Usage:
    from tools.skynet_dispatch_verify import verify_dispatch

    success = dispatch_to_worker("alpha", "fix the bug")
    if success:
        verified = verify_dispatch("alpha", "fix the bug")
        # verified == True means worker is confirmed PROCESSING
"""
import json
import time
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
AUDIT_FILE = ROOT / "data" / "dispatch_audit.json"
WORKERS_FILE = ROOT / "data" / "workers.json"

sys.path.insert(0, str(ROOT))


def _load_audit() -> list:
    if AUDIT_FILE.exists():
        try:
            return json.loads(AUDIT_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_audit(entries: list):
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Keep last 500 entries to prevent unbounded growth
    if len(entries) > 500:
        entries = entries[-500:]
    AUDIT_FILE.write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")


def _log_verification(worker: str, task_summary: str, pre_state: str,
                      post_state: str, outcome: str, retried: bool,
                      retry_outcome: str = None):
    entries = _load_audit()
    entries.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "worker": worker,
        "task_summary": task_summary[:200],
        "pre_state": pre_state,
        "post_state": post_state,
        "outcome": outcome,
        "retried": retried,
        "retry_outcome": retry_outcome,
    })
    _save_audit(entries)


def _get_worker_hwnd(worker_name: str) -> int:
    """Get worker HWND from workers.json."""
    if not WORKERS_FILE.exists():
        return 0
    try:
        data = json.loads(WORKERS_FILE.read_text(encoding="utf-8"))
        workers = data if isinstance(data, list) else data.get("workers", [])
        for w in workers:
            if w.get("name", "").lower() == worker_name.lower():
                return w.get("hwnd", 0)
    except (json.JSONDecodeError, OSError):
        pass
    return 0


def _get_state(hwnd: int) -> str:
    """Get worker state via UIA engine."""
    try:
        from tools.uia_engine import get_engine
        engine = get_engine()
        return engine.get_state(hwnd)
    except Exception:
        return "UNKNOWN"


def verify_dispatch(worker_name: str, task: str, wait_seconds: float = 10.0,
                    auto_redispatch: bool = True) -> bool:
    """Verify a dispatch was received by checking worker state after a delay.

    Args:
        worker_name: Target worker name (alpha/beta/gamma/delta).
        task: The task text that was dispatched (for logging/re-dispatch).
        wait_seconds: Seconds to wait before checking state (default 10).
        auto_redispatch: If True and worker still IDLE, re-dispatch once.

    Returns:
        True if worker confirmed PROCESSING (dispatch received).
        False if worker still IDLE after verification (dispatch failed).
    """
    hwnd = _get_worker_hwnd(worker_name)
    if not hwnd:
        _log_verification(worker_name, task, "NO_HWND", "NO_HWND",
                          "FAILED_NO_HWND", False)
        return False

    pre_state = _get_state(hwnd)
    time.sleep(wait_seconds)
    post_state = _get_state(hwnd)

    if post_state in ("PROCESSING", "TYPING"):
        _log_verification(worker_name, task, pre_state, post_state,
                          "VERIFIED", False)
        return True

    if post_state == "STEERING":
        _log_verification(worker_name, task, pre_state, post_state,
                          "VERIFIED_STEERING", False)
        return True

    # Worker still IDLE -- dispatch likely failed silently
    if not auto_redispatch:
        _log_verification(worker_name, task, pre_state, post_state,
                          "FAILED_STILL_IDLE", False)
        return False

    # Re-dispatch once
    print(f"[VERIFY] WARNING: {worker_name} still {post_state} after dispatch. Re-dispatching...")
    retry_success = _redispatch(worker_name, task, hwnd)
    retry_outcome = "RETRY_SUCCESS" if retry_success else "RETRY_FAILED"

    _log_verification(worker_name, task, pre_state, post_state,
                      "FAILED_RETRIED", True, retry_outcome)

    if retry_success:
        # Wait again and confirm
        time.sleep(wait_seconds)
        final_state = _get_state(hwnd)
        if final_state in ("PROCESSING", "TYPING", "STEERING"):
            return True

    return False


def _redispatch(worker_name: str, task: str, hwnd: int) -> bool:
    """Re-dispatch task to worker via ghost_type_to_worker."""
    try:
        orch_hwnd = _get_orchestrator_hwnd()
        from tools.skynet_dispatch import ghost_type_to_worker
        return ghost_type_to_worker(hwnd, task, orch_hwnd)
    except Exception as e:
        print(f"[VERIFY] Re-dispatch failed: {e}")
        return False


def _get_orchestrator_hwnd() -> int:
    orch_file = ROOT / "data" / "orchestrator.json"
    if orch_file.exists():
        try:
            data = json.loads(orch_file.read_text(encoding="utf-8"))
            return data.get("hwnd", 0)
        except (json.JSONDecodeError, OSError):
            pass
    return 0


def get_audit_stats() -> dict:
    """Return summary statistics from dispatch audit log."""
    entries = _load_audit()
    if not entries:
        return {"total": 0, "verified": 0, "failed": 0, "retried": 0}

    verified = sum(1 for e in entries if e.get("outcome", "").startswith("VERIFIED"))
    failed = sum(1 for e in entries if "FAILED" in e.get("outcome", ""))
    retried = sum(1 for e in entries if e.get("retried"))
    retry_success = sum(1 for e in entries if e.get("retry_outcome") == "RETRY_SUCCESS")

    return {
        "total": len(entries),
        "verified": verified,
        "failed": failed,
        "retried": retried,
        "retry_success": retry_success,
        "success_rate": round(verified / len(entries) * 100, 1) if entries else 0,
    }


def get_recent_failures(n: int = 10) -> list:
    """Return last N failed verifications."""
    entries = _load_audit()
    failures = [e for e in entries if "FAILED" in e.get("outcome", "")]
    return failures[-n:]


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Dispatch verification tool")
    parser.add_argument("--stats", action="store_true", help="Show audit statistics")
    parser.add_argument("--failures", type=int, default=0, help="Show last N failures")
    args = parser.parse_args()

    if args.stats:
        stats = get_audit_stats()
        print(json.dumps(stats, indent=2))
    elif args.failures:
        fails = get_recent_failures(args.failures)
        print(json.dumps(fails, indent=2, default=str))
    else:
        stats = get_audit_stats()
        print(f"Dispatch Audit: {stats['total']} total, {stats['verified']} verified, "
              f"{stats['failed']} failed, {stats['retried']} retried "
              f"({stats['success_rate']}% success rate)")
