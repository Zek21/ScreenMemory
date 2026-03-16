"""Contract Net Protocol (CNP) for intelligent task routing in Skynet.

Implements the classic CNP negotiation cycle:
  1. Manager (orchestrator) ANNOUNCES a task with required capabilities
  2. Workers evaluate the announcement and SUBMIT BIDS based on their
     specialization match, current load, and historical success rate
  3. Manager AWARDS the task to the highest bidder

Integrates with skynet_specialization.py for skill-based bid scoring and
the Skynet bus for announcement/bid message flow.

Usage:
    python tools/skynet_contract_net.py announce "Fix auth bug" --capabilities security,debugging
    python tools/skynet_contract_net.py bid <task_id> --worker gamma --score 0.85
    python tools/skynet_contract_net.py award <task_id>
    python tools/skynet_contract_net.py auto "Fix auth bug" --capabilities security
    python tools/skynet_contract_net.py status [task_id]
    python tools/skynet_contract_net.py history [--limit N]
"""
# signed: gamma

import json
import os
import sys
import time
import hashlib
import argparse
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
CNP_STATE_PATH = DATA_DIR / "cnp_state.json"
BUS_URL = "http://localhost:8420"

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

# Bid scoring weights
WEIGHT_SKILL = 0.40       # specialization skill_score
WEIGHT_SUCCESS = 0.25     # historical success_rate in category
WEIGHT_LOAD = 0.20        # inverse of current load (idle = high score)
WEIGHT_EXPERIENCE = 0.15  # tasks_completed in category (normalized)

# Timeouts
BID_WINDOW_S = 5.0        # seconds to collect bids before awarding
TASK_EXPIRY_S = 300.0     # tasks expire after 5 minutes without award

# Task lifecycle states
STATE_ANNOUNCED = "announced"
STATE_BIDDING = "bidding"
STATE_AWARDED = "awarded"
STATE_EXPIRED = "expired"
STATE_COMPLETED = "completed"
# signed: gamma


def _load_cnp_state() -> dict:
    """Load CNP state from disk."""
    if CNP_STATE_PATH.exists():
        with open(CNP_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"tasks": {}, "history": [], "version": 1}  # signed: gamma


def _save_cnp_state(state: dict) -> None:
    """Atomically save CNP state."""
    tmp = str(CNP_STATE_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(CNP_STATE_PATH))  # signed: gamma


def _generate_task_id(description: str) -> str:
    """Generate a short deterministic task ID from description + timestamp."""
    raw = f"{description}:{time.time()}"
    return "cnp_" + hashlib.sha256(raw.encode()).hexdigest()[:10]
    # signed: gamma


def _get_worker_load(worker: str) -> float:
    """Get worker's current load as 0.0 (idle) to 1.0 (fully busy).

    Reads from data/realtime.json if available, falls back to backend /status.
    """
    # Try realtime.json first (zero-network)
    rt_path = DATA_DIR / "realtime.json"
    if rt_path.exists():
        try:
            with open(rt_path, "r", encoding="utf-8") as f:
                rt = json.load(f)
            workers = rt.get("workers", {})
            w = workers.get(worker, {})
            status = w.get("status", "IDLE").upper()
            if status in ("IDLE", "FAILED"):
                return 0.0
            if status in ("ACTIVE", "BUSY", "PROCESSING"):
                return 0.8
            return 0.5  # UNKNOWN
        except (json.JSONDecodeError, OSError):
            pass

    # Fallback: try backend /status
    try:
        import urllib.request
        resp = urllib.request.urlopen(f"{BUS_URL}/status", timeout=3)
        data = json.loads(resp.read().decode())
        resp.close()
        agents = data.get("agents", [])
        for a in agents:
            if a.get("name", "").lower() == worker:
                status = a.get("status", "IDLE").upper()
                return 0.8 if status in ("ACTIVE", "BUSY", "PROCESSING") else 0.0
    except Exception:
        pass

    return 0.5  # Unknown state = moderate load assumption
    # signed: gamma


def _compute_bid_score(
    worker: str,
    required_capabilities: list[str],
) -> dict:
    """Compute an automatic bid score for a worker based on specialization data.

    Returns:
        Dict with score breakdown: {total, skill, success, load, experience, reasoning}
    """
    try:
        from tools.skynet_specialization import recommend_worker, get_specialization, TASK_CATEGORIES
    except ImportError:
        return {
            "total": 0.5, "skill": 0.0, "success": 0.0,
            "load": 0.5, "experience": 0.0,
            "reasoning": "skynet_specialization unavailable, using default score",
        }

    # Aggregate skill across all required capabilities
    skill_scores = []
    success_rates = []
    task_counts = []

    for cap in required_capabilities:
        cap_lower = cap.lower().strip()
        if cap_lower not in TASK_CATEGORIES:
            continue
        recs = recommend_worker(cap_lower)
        for r in recs:
            if r["worker"] == worker:
                skill_scores.append(r["skill_score"])
                success_rates.append(r["success_rate"])
                task_counts.append(r["tasks_completed"])
                break

    # Average across capabilities (or 0 if no data)
    avg_skill = sum(skill_scores) / len(skill_scores) if skill_scores else 0.0
    avg_success = sum(success_rates) / len(success_rates) if success_rates else 0.5
    total_tasks = sum(task_counts)
    # Normalize experience: 10+ tasks = 1.0
    norm_experience = min(1.0, total_tasks / 10.0)

    # Load: 0.0 = busy (bad), 1.0 = idle (good)
    load = _get_worker_load(worker)
    load_score = 1.0 - load

    # Weighted composite
    total = (
        WEIGHT_SKILL * avg_skill
        + WEIGHT_SUCCESS * avg_success
        + WEIGHT_LOAD * load_score
        + WEIGHT_EXPERIENCE * norm_experience
    )
    total = round(min(1.0, total), 4)

    # Build reasoning string
    caps_str = ", ".join(required_capabilities)
    parts = []
    if avg_skill > 0:
        parts.append(f"skill={avg_skill:.2f}")
    if total_tasks > 0:
        parts.append(f"exp={total_tasks} tasks")
    parts.append(f"load={'idle' if load < 0.2 else 'busy'}")
    reasoning = f"Capabilities [{caps_str}]: {', '.join(parts)}"

    return {
        "total": total,
        "skill": round(avg_skill, 4),
        "success": round(avg_success, 4),
        "load": round(load_score, 4),
        "experience": round(norm_experience, 4),
        "reasoning": reasoning,
    }  # signed: gamma


def announce_task(
    task_description: str,
    required_capabilities: list[str],
    announcer: str = "orchestrator",
) -> dict:
    """Announce a task for bidding via the Contract Net Protocol.

    Posts announcement to the Skynet bus and stores task state for bid collection.

    Args:
        task_description: Human-readable description of the task.
        required_capabilities: List of capability categories needed (from TASK_CATEGORIES).
        announcer: Who is announcing (default: orchestrator).

    Returns:
        Task record dict with task_id, state, and announcement details.
    """
    task_id = _generate_task_id(task_description)
    now = time.time()

    task_record = {
        "task_id": task_id,
        "description": task_description,
        "required_capabilities": required_capabilities,
        "announcer": announcer,
        "state": STATE_ANNOUNCED,
        "announced_at": now,
        "announced_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "bids": {},
        "winner": None,
        "expires_at": now + TASK_EXPIRY_S,
    }

    # Save to CNP state
    state = _load_cnp_state()
    state["tasks"][task_id] = task_record
    _save_cnp_state(state)

    # Publish announcement to bus
    try:
        from tools.skynet_spam_guard import guarded_publish
        guarded_publish({
            "sender": announcer,
            "topic": "workers",
            "type": "cnp_announcement",
            "content": json.dumps({
                "task_id": task_id,
                "description": task_description,
                "required_capabilities": required_capabilities,
                "bid_deadline_s": BID_WINDOW_S,
            }),
        })
    except Exception:
        pass  # Bus publish is best-effort; state file is authoritative

    return task_record  # signed: gamma


def submit_bid(
    worker: str,
    task_id: str,
    bid_score: Optional[float] = None,
    reasoning: Optional[str] = None,
) -> dict:
    """Submit a bid from a worker for an announced task.

    If bid_score is None, auto-computes from specialization data + load.

    Args:
        worker: Worker name submitting the bid.
        task_id: ID of the announced task.
        bid_score: Optional manual override score (0.0-1.0).
        reasoning: Optional manual reasoning string.

    Returns:
        Bid record dict.

    Raises:
        ValueError: If worker or task_id is invalid.
    """
    worker = worker.lower().strip()
    if worker not in WORKER_NAMES:
        raise ValueError(f"Unknown worker '{worker}'. Valid: {WORKER_NAMES}")

    state = _load_cnp_state()
    task = state["tasks"].get(task_id)
    if not task:
        raise ValueError(f"Unknown task_id '{task_id}'. Use 'status' to see active tasks.")

    if task["state"] not in (STATE_ANNOUNCED, STATE_BIDDING):
        raise ValueError(
            f"Task '{task_id}' is in state '{task['state']}', cannot accept bids."
        )

    # Auto-compute bid if score not provided
    if bid_score is None:
        auto = _compute_bid_score(worker, task["required_capabilities"])
        bid_score = auto["total"]
        if reasoning is None:
            reasoning = auto["reasoning"]
    else:
        bid_score = max(0.0, min(1.0, bid_score))
        if reasoning is None:
            reasoning = "Manual bid"

    bid_record = {
        "worker": worker,
        "score": round(bid_score, 4),
        "reasoning": reasoning,
        "submitted_at": time.time(),
        "submitted_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    # Store bid
    task["bids"][worker] = bid_record
    task["state"] = STATE_BIDDING
    state["tasks"][task_id] = task
    _save_cnp_state(state)

    # Publish bid to bus
    try:
        from tools.skynet_spam_guard import guarded_publish
        guarded_publish({
            "sender": worker,
            "topic": "workers",
            "type": "cnp_bid",
            "content": json.dumps({
                "task_id": task_id,
                "worker": worker,
                "score": bid_record["score"],
                "reasoning": reasoning,
            }),
        })
    except Exception:
        pass

    return bid_record  # signed: gamma


def award_task(task_id: str) -> dict:
    """Award a task to the highest bidder.

    Selects the bid with the highest score. On tie, prefers the worker
    who bid first (earliest submitted_at).

    Args:
        task_id: ID of the task to award.

    Returns:
        Award result dict with winner and all bids.

    Raises:
        ValueError: If task has no bids or is in wrong state.
    """
    state = _load_cnp_state()
    task = state["tasks"].get(task_id)
    if not task:
        raise ValueError(f"Unknown task_id '{task_id}'.")

    if task["state"] not in (STATE_ANNOUNCED, STATE_BIDDING):
        raise ValueError(f"Task '{task_id}' is in state '{task['state']}', cannot award.")

    bids = task.get("bids", {})
    if not bids:
        raise ValueError(f"Task '{task_id}' has no bids. Cannot award.")

    # Sort bids: highest score first, earliest submission as tiebreaker
    ranked = sorted(
        bids.values(),
        key=lambda b: (-b["score"], b["submitted_at"]),
    )

    winner = ranked[0]
    task["state"] = STATE_AWARDED
    task["winner"] = winner["worker"]
    task["awarded_at"] = time.time()
    task["awarded_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    # Move to history
    state["history"].append({
        "task_id": task_id,
        "description": task["description"],
        "capabilities": task["required_capabilities"],
        "winner": winner["worker"],
        "winning_score": winner["score"],
        "num_bids": len(bids),
        "awarded_at": task["awarded_at_iso"],
    })
    # Keep history bounded
    if len(state["history"]) > 200:
        state["history"] = state["history"][-200:]

    state["tasks"][task_id] = task
    _save_cnp_state(state)

    # Publish award to bus
    try:
        from tools.skynet_spam_guard import guarded_publish
        guarded_publish({
            "sender": "orchestrator",
            "topic": "workers",
            "type": "cnp_award",
            "content": json.dumps({
                "task_id": task_id,
                "winner": winner["worker"],
                "winning_score": winner["score"],
                "all_bids": {w: b["score"] for w, b in bids.items()},
            }),
        })
    except Exception:
        pass

    return {
        "task_id": task_id,
        "winner": winner["worker"],
        "winning_score": winner["score"],
        "winning_reasoning": winner["reasoning"],
        "all_bids": ranked,
        "num_bids": len(bids),
    }  # signed: gamma


def auto_route(
    task_description: str,
    required_capabilities: list[str],
    announcer: str = "orchestrator",
) -> dict:
    """Full CNP cycle in one call: announce → auto-bid all workers → award.

    Convenience function that runs the complete protocol synchronously.
    All workers bid automatically based on their specialization profiles.

    Args:
        task_description: Task to route.
        required_capabilities: Required capability categories.
        announcer: Who initiated.

    Returns:
        Award result with winner and bid breakdown.
    """
    # Step 1: Announce
    task = announce_task(task_description, required_capabilities, announcer)
    task_id = task["task_id"]

    # Step 2: Collect bids from all workers (auto-computed)
    for worker in WORKER_NAMES:
        try:
            submit_bid(worker, task_id)
        except Exception:
            pass  # Skip workers that can't bid

    # Step 3: Award to highest bidder
    return award_task(task_id)  # signed: gamma


def get_status(task_id: Optional[str] = None) -> dict:
    """Get status of a specific task or all active tasks.

    Args:
        task_id: Specific task to query, or None for all active.

    Returns:
        Task record(s) with current state and bids.
    """
    state = _load_cnp_state()
    now = time.time()

    # Expire old tasks
    for tid, task in list(state["tasks"].items()):
        if task["state"] in (STATE_ANNOUNCED, STATE_BIDDING):
            if now > task.get("expires_at", now + 1):
                task["state"] = STATE_EXPIRED
                state["tasks"][tid] = task

    _save_cnp_state(state)

    if task_id:
        task = state["tasks"].get(task_id)
        if not task:
            return {"error": f"Unknown task_id '{task_id}'"}
        return task

    # Return all non-expired active tasks
    active = {
        tid: t for tid, t in state["tasks"].items()
        if t["state"] in (STATE_ANNOUNCED, STATE_BIDDING, STATE_AWARDED)
    }
    return {"active_tasks": len(active), "tasks": active}
    # signed: gamma


def get_history(limit: int = 20) -> list[dict]:
    """Get recent CNP award history."""
    state = _load_cnp_state()
    return state.get("history", [])[-limit:]  # signed: gamma


def _cli():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Skynet Contract Net Protocol for intelligent task routing"
    )
    sub = parser.add_subparsers(dest="command")

    # announce
    ann = sub.add_parser("announce", help="Announce a task for bidding")
    ann.add_argument("description", help="Task description")
    ann.add_argument(
        "--capabilities", required=True,
        help="Comma-separated required capabilities",
    )
    ann.add_argument("--announcer", default="orchestrator")

    # bid
    bid_p = sub.add_parser("bid", help="Submit a bid for a task")
    bid_p.add_argument("task_id", help="Task ID to bid on")
    bid_p.add_argument("--worker", required=True, help="Worker name")
    bid_p.add_argument("--score", type=float, default=None, help="Manual bid score")
    bid_p.add_argument("--reasoning", default=None, help="Bid reasoning")

    # award
    aw = sub.add_parser("award", help="Award task to highest bidder")
    aw.add_argument("task_id", help="Task ID to award")

    # auto
    auto_p = sub.add_parser("auto", help="Full CNP cycle: announce+bid+award")
    auto_p.add_argument("description", help="Task description")
    auto_p.add_argument(
        "--capabilities", required=True,
        help="Comma-separated required capabilities",
    )

    # status
    st = sub.add_parser("status", help="Show task status")
    st.add_argument("task_id", nargs="?", default=None)

    # history
    hi = sub.add_parser("history", help="Show award history")
    hi.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()

    if args.command == "announce":
        caps = [c.strip() for c in args.capabilities.split(",")]
        result = announce_task(args.description, caps, args.announcer)
        print(f"Task announced: {result['task_id']}")
        print(f"  Description: {result['description']}")
        print(f"  Capabilities: {result['required_capabilities']}")
        print(f"  Expires in: {TASK_EXPIRY_S}s")

    elif args.command == "bid":
        result = submit_bid(args.worker, args.task_id, args.score, args.reasoning)
        print(f"Bid submitted: {result['worker']} -> score={result['score']:.4f}")
        print(f"  Reasoning: {result['reasoning']}")

    elif args.command == "award":
        result = award_task(args.task_id)
        print(f"AWARDED to {result['winner']} (score={result['winning_score']:.4f})")
        print(f"  Reasoning: {result['winning_reasoning']}")
        print(f"  Bids received: {result['num_bids']}")
        for b in result["all_bids"]:
            marker = " <-- WINNER" if b["worker"] == result["winner"] else ""
            print(f"    {b['worker']}: {b['score']:.4f}{marker}")

    elif args.command == "auto":
        caps = [c.strip() for c in args.capabilities.split(",")]
        result = auto_route(args.description, caps)
        print(f"AUTO-ROUTED to {result['winner']} (score={result['winning_score']:.4f})")
        print(f"  Reasoning: {result['winning_reasoning']}")
        for b in result["all_bids"]:
            marker = " <-- WINNER" if b["worker"] == result["winner"] else ""
            print(f"    {b['worker']}: {b['score']:.4f}{marker}")

    elif args.command == "status":
        result = get_status(args.task_id)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "history":
        hist = get_history(args.limit)
        if not hist:
            print("No CNP history yet.")
        else:
            for h in hist:
                print(
                    f"  {h['awarded_at']} | {h['winner']} won "
                    f"(score={h['winning_score']:.3f}, {h['num_bids']} bids) "
                    f"| {h['description'][:60]}"
                )

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: gamma
