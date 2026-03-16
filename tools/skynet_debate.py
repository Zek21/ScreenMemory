#!/usr/bin/env python3
"""Skynet Red Team / Blue Team Debate Framework (P2.06).

Implements adversarial debate for high-stakes tasks. A proposer writes a
solution, an attacker finds flaws, a defender revises, and a judge scores
the final output.  Each round is published to the Skynet bus so the
orchestrator and dashboard can follow the debate in real time.

Usage:
    python tools/skynet_debate.py --task "Design a caching layer" --rounds 3
    python tools/skynet_debate.py --score SESSION_ID
    python tools/skynet_debate.py --status
    python tools/skynet_debate.py --history

Integration:
    python tools/skynet_dispatch.py --debate --task "complex goal"
"""
# signed: beta

import json
import os
import time
import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DEBATES_FILE = DATA_DIR / "debate_sessions.json"

# ── Role constants ──────────────────────────────────────────────────
ROLE_PROPOSER = "proposer"
ROLE_ATTACKER = "attacker"
ROLE_DEFENDER = "defender"
ROLE_JUDGE = "judge"
ALL_ROLES = [ROLE_PROPOSER, ROLE_ATTACKER, ROLE_DEFENDER, ROLE_JUDGE]

# ── Defaults ────────────────────────────────────────────────────────
DEFAULT_ROUNDS = 3
MAX_ROUNDS = 10
MIN_QUALITY_SCORE = 0.0
MAX_QUALITY_SCORE = 10.0

# ── Worker pool for role assignment ─────────────────────────────────
WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

_lock = threading.Lock()


# ────────────────────────────────────────────────────────────────────
# Persistence helpers
# ────────────────────────────────────────────────────────────────────
def _load_sessions() -> dict:
    """Load all debate sessions from disk."""
    if not DEBATES_FILE.exists():
        return {}
    try:
        with open(DEBATES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_sessions(sessions: dict) -> None:
    """Atomically persist debate sessions."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DEBATES_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sessions, f, indent=2, default=str)
    os.replace(str(tmp), str(DEBATES_FILE))


def _generate_session_id(task: str) -> str:
    """Create a short, unique session id from task text + timestamp."""
    digest = hashlib.sha256(f"{task}{time.time()}".encode()).hexdigest()[:8]
    return f"debate_{digest}"


# ────────────────────────────────────────────────────────────────────
# Bus publishing helper
# ────────────────────────────────────────────────────────────────────
def _bus_publish(sender: str, msg_type: str, content: str,
                 metadata: dict | None = None) -> bool:
    """Publish a debate event to the Skynet bus via SpamGuard."""
    try:
        from tools.skynet_spam_guard import guarded_publish
        payload = {
            "sender": sender,
            "topic": "debate",
            "type": msg_type,
            "content": content,
        }
        if metadata:
            payload["metadata"] = metadata
        return guarded_publish(payload)
    except Exception:
        return False


# ────────────────────────────────────────────────────────────────────
# Role assignment
# ────────────────────────────────────────────────────────────────────
def assign_roles(workers: list[str] | None = None) -> dict[str, str]:
    """Map debate roles to available workers.

    With 4 workers the mapping is 1:1.  With fewer, one worker may hold
    multiple roles (proposer doubles as defender, attacker doubles as
    judge).
    """
    pool = list(workers or WORKER_NAMES)
    assignment: dict[str, str] = {}

    if len(pool) >= 4:
        assignment[ROLE_PROPOSER] = pool[0]
        assignment[ROLE_ATTACKER] = pool[1]
        assignment[ROLE_DEFENDER] = pool[2]
        assignment[ROLE_JUDGE] = pool[3]
    elif len(pool) == 3:
        assignment[ROLE_PROPOSER] = pool[0]
        assignment[ROLE_ATTACKER] = pool[1]
        assignment[ROLE_DEFENDER] = pool[2]
        assignment[ROLE_JUDGE] = pool[1]  # attacker also judges
    elif len(pool) == 2:
        assignment[ROLE_PROPOSER] = pool[0]
        assignment[ROLE_ATTACKER] = pool[1]
        assignment[ROLE_DEFENDER] = pool[0]  # proposer defends
        assignment[ROLE_JUDGE] = pool[1]  # attacker judges
    else:
        # Single worker plays all roles (degenerate but functional)
        assignment = {r: pool[0] for r in ALL_ROLES}

    return assignment


# ────────────────────────────────────────────────────────────────────
# DebateSession
# ────────────────────────────────────────────────────────────────────
class DebateSession:
    """Manages a multi-round adversarial debate on a given task.

    Lifecycle:
        1. ``start()`` — creates session, assigns roles, publishes round_0
        2. ``submit_round(role, content)`` — records a round contribution
        3. ``judge_solution(verdict, score, rationale)`` — final evaluation
        4. ``score_debate()`` — computes quality-improvement metric
    """

    def __init__(self, task: str, rounds: int = DEFAULT_ROUNDS,
                 workers: list[str] | None = None,
                 session_id: str | None = None):
        self.session_id = session_id or _generate_session_id(task)
        self.task = task
        self.total_rounds = min(max(rounds, 1), MAX_ROUNDS)
        self.roles = assign_roles(workers)
        self.rounds: list[dict] = []
        self.status = "created"
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.completed_at: str | None = None
        self.verdict: dict | None = None

    # ── Serialisation ───────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "task": self.task,
            "total_rounds": self.total_rounds,
            "roles": self.roles,
            "rounds": self.rounds,
            "status": self.status,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "verdict": self.verdict,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DebateSession":
        s = cls.__new__(cls)
        s.session_id = d["session_id"]
        s.task = d["task"]
        s.total_rounds = d.get("total_rounds", DEFAULT_ROUNDS)
        s.roles = d["roles"]
        s.rounds = d.get("rounds", [])
        s.status = d.get("status", "created")
        s.created_at = d.get("created_at", "")
        s.completed_at = d.get("completed_at")
        s.verdict = d.get("verdict")
        return s

    # ── Session lifecycle ───────────────────────────────────────────
    def start(self) -> dict:
        """Initialise the debate and publish the opening round to the bus."""
        self.status = "active"
        opening = {
            "round": 0,
            "role": "system",
            "worker": "system",
            "content": f"Debate opened: {self.task}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.rounds.append(opening)
        self._persist()
        _bus_publish(
            sender="system",
            msg_type="round_0",
            content=f"Debate {self.session_id} started: {self.task[:120]}",
            metadata={"session_id": self.session_id, "roles": self.roles},
        )
        return self.to_dict()

    def submit_round(self, role: str, content: str,
                     worker: str | None = None) -> dict:
        """Record a round contribution from a role.

        Args:
            role: One of proposer / attacker / defender / judge.
            content: The contribution text.
            worker: Worker name (auto-resolved from roles if omitted).

        Returns:
            The round record dict.
        """
        if role not in ALL_ROLES:
            raise ValueError(f"Invalid role '{role}'; must be one of {ALL_ROLES}")

        round_num = len(self.rounds)
        worker = worker or self.roles.get(role, "unknown")

        record = {
            "round": round_num,
            "role": role,
            "worker": worker,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.rounds.append(record)

        msg_type = f"round_{round_num}"
        _bus_publish(
            sender=worker,
            msg_type=msg_type,
            content=f"[{self.session_id}] {role.upper()}: {content[:200]}",
            metadata={"session_id": self.session_id, "role": role,
                       "round": round_num},
        )
        self._persist()
        return record

    def judge_solution(self, verdict: str, score: float,
                       rationale: str) -> dict:
        """Record the judge's final evaluation.

        Args:
            verdict: 'approve', 'revise', or 'reject'.
            score: Quality score 0-10.
            rationale: Judge's reasoning.

        Returns:
            The verdict dict.
        """
        score = max(MIN_QUALITY_SCORE, min(MAX_QUALITY_SCORE, score))
        self.verdict = {
            "verdict": verdict,
            "score": score,
            "rationale": rationale,
            "judge": self.roles.get(ROLE_JUDGE, "unknown"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.status = "completed"
        self.completed_at = datetime.now(timezone.utc).isoformat()
        self._persist()

        _bus_publish(
            sender=self.roles.get(ROLE_JUDGE, "system"),
            msg_type="verdict",
            content=(f"[{self.session_id}] VERDICT={verdict} "
                     f"SCORE={score:.1f}: {rationale[:200]}"),
            metadata={"session_id": self.session_id,
                       "verdict": verdict, "score": score},
        )
        return self.verdict

    # ── Persistence ─────────────────────────────────────────────────
    def _persist(self) -> None:
        """Save this session to the shared debates file."""
        with _lock:
            sessions = _load_sessions()
            sessions[self.session_id] = self.to_dict()
            _save_sessions(sessions)

    # ── Convenience queries ─────────────────────────────────────────
    @property
    def current_round(self) -> int:
        return len(self.rounds)

    @property
    def is_complete(self) -> bool:
        return self.status == "completed"

    def get_proposal(self) -> str | None:
        """Return the proposer's initial solution text."""
        for r in self.rounds:
            if r.get("role") == ROLE_PROPOSER:
                return r["content"]
        return None

    def get_attacks(self) -> list[str]:
        """Return all attacker contributions."""
        return [r["content"] for r in self.rounds
                if r.get("role") == ROLE_ATTACKER]

    def get_defense(self) -> str | None:
        """Return the defender's revised solution."""
        for r in reversed(self.rounds):
            if r.get("role") == ROLE_DEFENDER:
                return r["content"]
        return None


# ────────────────────────────────────────────────────────────────────
# High-level API
# ────────────────────────────────────────────────────────────────────
def debate(task: str, rounds: int = DEFAULT_ROUNDS,
           workers: list[str] | None = None) -> DebateSession:
    """Run a full Red Team / Blue Team debate.

    Round 1 — Proposer writes initial solution.
    Round 2 — Attacker identifies flaws and weaknesses.
    Round 3 — Defender produces a revised solution addressing the flaws.
    (Extra rounds alternate attacker / defender.)

    The judge evaluates the final output and scores quality.

    In autonomous mode each role is a *prompt template* that the
    assigned worker receives via dispatch.  This function creates the
    session and generates the prompts; actual worker execution happens
    through the normal dispatch pipeline.

    Returns:
        The (in-progress) DebateSession.  Callers should check
        ``session.is_complete`` or poll the bus for the verdict.
    """
    session = DebateSession(task, rounds=rounds, workers=workers)
    session.start()

    # Build dispatch prompts for each round
    prompts = _build_round_prompts(session)
    session._prompts = prompts  # store for dispatch integration

    return session


def _build_round_prompts(session: DebateSession) -> list[dict]:
    """Generate role-specific prompts for each debate round."""
    prompts: list[dict] = []
    task = session.task
    sid = session.session_id

    # Round 1 — Proposer
    prompts.append({
        "round": 1,
        "role": ROLE_PROPOSER,
        "worker": session.roles[ROLE_PROPOSER],
        "prompt": (
            f"DEBATE {sid} — PROPOSER (Round 1)\n"
            f"Task: {task}\n\n"
            f"Write a complete, detailed solution for the task above. "
            f"Cover architecture, implementation steps, edge cases, and "
            f"trade-offs. Post your solution to the bus with:\n"
            f"  topic=debate type=round_1 "
            f"  metadata={{session_id: '{sid}', role: 'proposer'}}\n"
            f"signed:{{your_name}}"
        ),
    })

    # Rounds 2..N-1 — alternate attacker / defender
    for rnd in range(2, session.total_rounds):
        if rnd % 2 == 0:
            role = ROLE_ATTACKER
            instruction = (
                f"DEBATE {sid} — ATTACKER (Round {rnd})\n"
                f"Task: {task}\n\n"
                f"Read the proposer's solution from the bus "
                f"(topic=debate, session_id={sid}, round_{rnd-1}).\n"
                f"Find weaknesses, security flaws, missed edge cases, "
                f"performance issues, and logical errors. Be rigorous. "
                f"List every flaw with severity (CRITICAL/HIGH/MEDIUM/LOW).\n"
                f"Post your critique to the bus with:\n"
                f"  topic=debate type=round_{rnd} "
                f"  metadata={{session_id: '{sid}', role: 'attacker'}}\n"
                f"signed:{{your_name}}"
            )
        else:
            role = ROLE_DEFENDER
            instruction = (
                f"DEBATE {sid} — DEFENDER (Round {rnd})\n"
                f"Task: {task}\n\n"
                f"Read the attacker's critique from the bus "
                f"(topic=debate, session_id={sid}, round_{rnd-1}).\n"
                f"Produce a REVISED solution that addresses every flaw. "
                f"For each flaw: acknowledge, explain your fix, or argue "
                f"why it is not applicable. The revised solution must be "
                f"strictly better than the original.\n"
                f"Post your revised solution to the bus with:\n"
                f"  topic=debate type=round_{rnd} "
                f"  metadata={{session_id: '{sid}', role: 'defender'}}\n"
                f"signed:{{your_name}}"
            )
        prompts.append({
            "round": rnd,
            "role": role,
            "worker": session.roles[role],
            "prompt": instruction,
        })

    # Final round — Judge
    prompts.append({
        "round": session.total_rounds,
        "role": ROLE_JUDGE,
        "worker": session.roles[ROLE_JUDGE],
        "prompt": (
            f"DEBATE {sid} — JUDGE (Final Round {session.total_rounds})\n"
            f"Task: {task}\n\n"
            f"Read ALL debate rounds from the bus "
            f"(topic=debate, session_id={sid}).\n"
            f"Evaluate the final solution on these criteria:\n"
            f"  1. Correctness — does it solve the task?\n"
            f"  2. Completeness — are edge cases handled?\n"
            f"  3. Security — are there vulnerabilities?\n"
            f"  4. Performance — is it efficient?\n"
            f"  5. Maintainability — is it clean and documented?\n\n"
            f"Provide:\n"
            f"  - VERDICT: approve / revise / reject\n"
            f"  - SCORE: 0-10 (overall quality)\n"
            f"  - RATIONALE: brief justification\n"
            f"Post your verdict to the bus with:\n"
            f"  topic=debate type=verdict "
            f"  metadata={{session_id: '{sid}', role: 'judge'}}\n"
            f"signed:{{your_name}}"
        ),
    })

    return prompts


def dispatch_debate(task: str, rounds: int = DEFAULT_ROUNDS,
                    workers: list[str] | None = None) -> dict:
    """Create a debate session AND dispatch all rounds to workers.

    Each round is dispatched sequentially with a dependency on the
    previous round's bus result.  The orchestrator can also dispatch
    rounds manually for tighter control.

    Returns:
        Summary dict with session_id, role assignments, and prompt count.
    """
    session = debate(task, rounds=rounds, workers=workers)
    prompts = getattr(session, "_prompts", [])

    dispatched = []
    try:
        from tools.skynet_dispatch import dispatch_to_worker
        for p in prompts:
            ok = dispatch_to_worker(p["worker"], p["prompt"])
            dispatched.append({
                "round": p["round"],
                "role": p["role"],
                "worker": p["worker"],
                "dispatched": bool(ok),
            })
            time.sleep(2.0)  # clipboard cooldown between dispatches
    except ImportError:
        # If dispatch not available, just record prompts
        for p in prompts:
            dispatched.append({
                "round": p["round"],
                "role": p["role"],
                "worker": p["worker"],
                "dispatched": False,
            })

    return {
        "session_id": session.session_id,
        "task": task,
        "roles": session.roles,
        "rounds_planned": session.total_rounds,
        "dispatched": dispatched,
    }


# ────────────────────────────────────────────────────────────────────
# Scoring
# ────────────────────────────────────────────────────────────────────
def score_debate(session_id: str) -> dict:
    """Calculate quality-improvement metrics for a completed debate.

    Metrics:
        - proposal_length: size of initial proposal
        - defense_length: size of revised solution
        - attack_count: number of flaws found
        - improvement_ratio: defense_length / proposal_length
        - judge_score: 0-10 from the judge (if available)
        - quality_delta: estimated improvement (judge_score - baseline 5.0)
    """
    sessions = _load_sessions()
    data = sessions.get(session_id)
    if not data:
        return {"error": f"Session {session_id} not found"}

    session = DebateSession.from_dict(data)

    proposal = session.get_proposal() or ""
    attacks = session.get_attacks()
    defense = session.get_defense() or ""
    judge_score = session.verdict["score"] if session.verdict else None

    baseline = 5.0  # assumed starting quality without debate
    quality_delta = (judge_score - baseline) if judge_score is not None else None

    improvement_ratio = (len(defense) / len(proposal)) if proposal else 0.0

    return {
        "session_id": session_id,
        "task": session.task,
        "status": session.status,
        "proposal_length": len(proposal),
        "defense_length": len(defense),
        "attack_count": len(attacks),
        "total_flaws_text_length": sum(len(a) for a in attacks),
        "improvement_ratio": round(improvement_ratio, 2),
        "judge_score": judge_score,
        "quality_delta": round(quality_delta, 2) if quality_delta is not None else None,
        "verdict": session.verdict["verdict"] if session.verdict else None,
        "rounds_completed": len(session.rounds),
    }


# ────────────────────────────────────────────────────────────────────
# Query helpers
# ────────────────────────────────────────────────────────────────────
def get_session(session_id: str) -> DebateSession | None:
    """Load a debate session by id."""
    sessions = _load_sessions()
    data = sessions.get(session_id)
    return DebateSession.from_dict(data) if data else None


def list_sessions(status: str | None = None) -> list[dict]:
    """List all debate sessions, optionally filtered by status."""
    sessions = _load_sessions()
    result = []
    for sid, data in sessions.items():
        if status and data.get("status") != status:
            continue
        result.append({
            "session_id": sid,
            "task": data.get("task", "")[:80],
            "status": data.get("status", "unknown"),
            "rounds": len(data.get("rounds", [])),
            "created_at": data.get("created_at", ""),
        })
    return result


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────
def _cli():
    import argparse
    parser = argparse.ArgumentParser(
        description="Skynet Debate -- Red Team / Blue Team framework",
    )
    parser.add_argument("--task", type=str, help="Task to debate")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS,
                        help=f"Number of debate rounds (default {DEFAULT_ROUNDS})")
    parser.add_argument("--dispatch", action="store_true",
                        help="Create session AND dispatch to workers")
    parser.add_argument("--score", type=str,
                        help="Score a completed debate session by id")
    parser.add_argument("--status", action="store_true",
                        help="List all debate sessions")
    parser.add_argument("--history", action="store_true",
                        help="Show completed debates with scores")
    parser.add_argument("--workers", type=str,
                        help="Comma-separated worker list override")
    args = parser.parse_args()

    if args.score:
        result = score_debate(args.score)
        print(json.dumps(result, indent=2))
        return

    if args.status:
        sessions = list_sessions()
        if not sessions:
            print("No debate sessions found.")
            return
        for s in sessions:
            print(f"  {s['session_id']}  [{s['status']:>9}]  "
                  f"rounds={s['rounds']}  {s['task']}")
        return

    if args.history:
        sessions = list_sessions(status="completed")
        if not sessions:
            print("No completed debates found.")
            return
        for s in sessions:
            sc = score_debate(s["session_id"])
            js = sc.get("judge_score", "N/A")
            v = sc.get("verdict", "N/A")
            print(f"  {s['session_id']}  score={js}  verdict={v}  {s['task']}")
        return

    if args.task:
        workers = args.workers.split(",") if args.workers else None
        if args.dispatch:
            result = dispatch_debate(args.task, rounds=args.rounds,
                                     workers=workers)
        else:
            session = debate(args.task, rounds=args.rounds, workers=workers)
            result = session.to_dict()
            result["prompts"] = [
                {"round": p["round"], "role": p["role"], "worker": p["worker"]}
                for p in getattr(session, "_prompts", [])
            ]
        print(json.dumps(result, indent=2))
        return

    parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: beta
