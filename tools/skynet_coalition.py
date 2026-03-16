"""Skynet Coalition Formation — Squad Mode (P3.03).

Dynamically groups workers into temporary squads (coalitions) for complex
tasks.  Three formation strategies select members:

  * **skill_based**  — pick workers whose specialization matches the goal
  * **load_based**   — pick workers that are currently IDLE
  * **affinity_based** — pick workers that historically collaborate well

Coalitions communicate through a dedicated bus channel (topic=workers,
type=coalition_share, metadata.coalition_id=<id>) and auto-dissolve when
the shared goal is achieved.

CLI
---
    python tools/skynet_coalition.py form   --goal GOAL [--strategy skill|load|affinity] [--size N]
    python tools/skynet_coalition.py status [--id COALITION_ID]
    python tools/skynet_coalition.py dissolve --id COALITION_ID [--reason REASON]
    python tools/skynet_coalition.py history [--limit N]
    python tools/skynet_coalition.py share   --id COALITION_ID --content TEXT --sender WORKER
    python tools/skynet_coalition.py read    --id COALITION_ID [--limit N]

State: data/coalitions.json
"""
# signed: gamma
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# ── Paths ────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
COALITIONS_PATH = DATA_DIR / "coalitions.json"
PROFILES_PATH = DATA_DIR / "agent_profiles.json"

# ── Constants ────────────────────────────────────────────────────
WORKER_NAMES: list[str] = ["alpha", "beta", "gamma", "delta"]
SKYNET_PORT = 8420
BUS_URL = f"http://localhost:{SKYNET_PORT}"

MAX_COALITION_SIZE = 4
DEFAULT_COALITION_SIZE = 2
MAX_HISTORY = 200
COALITION_EXPIRE_S = 3600  # auto-dissolve after 1 hour if not completed
MIN_AFFINITY_INTERACTIONS = 2  # minimum shared coalitions for affinity signal

# Categories recognised by the specialization tracker
# Must match tools/skynet_specialization.py TASK_CATEGORIES
KNOWN_CATEGORIES: list[str] = [
    "security", "testing", "refactoring", "documentation",
    "infrastructure", "frontend", "backend", "performance",
    "debugging", "architecture", "code_review", "deployment",
    "monitoring", "wiring", "research",
]
DEFAULT_CATEGORY = "backend"  # fallback when no category matched


# ── Enums ────────────────────────────────────────────────────────

class FormationStrategy(Enum):
    """How members are selected for a coalition."""
    SKILL_BASED = "skill"
    LOAD_BASED = "load"
    AFFINITY_BASED = "affinity"


class CoalitionStatus(Enum):
    """Lifecycle states of a coalition."""
    PROPOSED = "proposed"
    ACTIVE = "active"
    COMPLETED = "completed"
    DISSOLVED = "dissolved"
    EXPIRED = "expired"


# ── Data Classes ─────────────────────────────────────────────────

@dataclass
class CoalitionMember:
    """A worker participating in a coalition."""
    worker: str
    joined_at: float = 0.0
    role: str = "member"  # "leader" | "member"
    contributions: int = 0

    def to_dict(self) -> dict:
        return {
            "worker": self.worker,
            "joined_at": self.joined_at,
            "role": self.role,
            "contributions": self.contributions,
        }

    @classmethod
    def from_dict(cls, d: dict) -> CoalitionMember:
        return cls(
            worker=d["worker"],
            joined_at=d.get("joined_at", 0.0),
            role=d.get("role", "member"),
            contributions=d.get("contributions", 0),
        )


@dataclass
class Coalition:
    """A temporary squad of workers pursuing a shared goal."""
    coalition_id: str
    goal: str
    strategy: FormationStrategy
    status: CoalitionStatus = CoalitionStatus.PROPOSED
    members: list[CoalitionMember] = field(default_factory=list)
    leader: str = ""
    created_at: float = 0.0
    activated_at: float = 0.0
    dissolved_at: float = 0.0
    dissolve_reason: str = ""
    shared_results: list[dict] = field(default_factory=list)
    # Metadata
    categories: list[str] = field(default_factory=list)
    max_size: int = MAX_COALITION_SIZE

    @property
    def member_names(self) -> list[str]:
        return [m.worker for m in self.members]

    @property
    def bus_topic_filter(self) -> str:
        """Metadata filter for bus messages in this coalition."""
        return self.coalition_id

    @property
    def age_s(self) -> float:
        if self.created_at <= 0:
            return 0.0
        return time.time() - self.created_at

    @property
    def is_expired(self) -> bool:
        return (self.status == CoalitionStatus.ACTIVE
                and self.age_s > COALITION_EXPIRE_S)

    def add_member(self, worker: str, role: str = "member") -> bool:
        if worker in self.member_names:
            return False
        if len(self.members) >= self.max_size:
            return False
        self.members.append(CoalitionMember(
            worker=worker,
            joined_at=time.time(),
            role=role,
        ))
        return True

    def remove_member(self, worker: str) -> bool:
        before = len(self.members)
        self.members = [m for m in self.members if m.worker != worker]
        return len(self.members) < before

    def record_contribution(self, worker: str) -> None:
        for m in self.members:
            if m.worker == worker:
                m.contributions += 1
                break

    def to_dict(self) -> dict:
        return {
            "coalition_id": self.coalition_id,
            "goal": self.goal,
            "strategy": self.strategy.value,
            "status": self.status.value,
            "members": [m.to_dict() for m in self.members],
            "leader": self.leader,
            "created_at": self.created_at,
            "activated_at": self.activated_at,
            "dissolved_at": self.dissolved_at,
            "dissolve_reason": self.dissolve_reason,
            "shared_results": self.shared_results[-50:],
            "categories": self.categories,
            "max_size": self.max_size,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Coalition:
        c = cls(
            coalition_id=d["coalition_id"],
            goal=d["goal"],
            strategy=FormationStrategy(d.get("strategy", "skill")),
            status=CoalitionStatus(d.get("status", "proposed")),
            leader=d.get("leader", ""),
            created_at=d.get("created_at", 0.0),
            activated_at=d.get("activated_at", 0.0),
            dissolved_at=d.get("dissolved_at", 0.0),
            dissolve_reason=d.get("dissolve_reason", ""),
            shared_results=d.get("shared_results", []),
            categories=d.get("categories", []),
            max_size=d.get("max_size", DEFAULT_COALITION_SIZE),
        )
        c.members = [CoalitionMember.from_dict(m)
                      for m in d.get("members", [])]
        return c


# ── Helpers ──────────────────────────────────────────────────────

def _generate_id(goal: str) -> str:
    """Deterministic but unique coalition ID from goal + timestamp."""
    raw = f"{goal}:{time.time()}"
    return "coa_" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def _load_state() -> dict:
    """Load coalition state from disk."""
    if COALITIONS_PATH.exists():
        try:
            with open(COALITIONS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"active": {}, "history": [], "affinity": {}, "version": 1}


def _save_state(state: dict) -> None:
    """Persist coalition state to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = COALITIONS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(COALITIONS_PATH)


def _load_profiles() -> dict:
    """Load agent profiles."""
    try:
        with open(PROFILES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _get_worker_states() -> dict[str, str]:
    """Query backend for current worker states."""
    try:
        req = urllib.request.Request(f"{BUS_URL}/status", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        agents = data.get("agents", [])
        result: dict[str, str] = {}
        for a in agents:
            name = a.get("name", "")
            if name in WORKER_NAMES:
                result[name] = a.get("status", "UNKNOWN")
        return result
    except Exception:
        return {w: "UNKNOWN" for w in WORKER_NAMES}


def _bus_publish(msg: dict) -> bool:
    """Publish a message to the bus via guarded_publish."""
    try:
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish(msg)
        return result.get("published", False)
    except Exception:
        return False


def _extract_categories(goal: str) -> list[str]:
    """Extract likely task categories from a goal description."""
    goal_lower = goal.lower()
    found = []
    for cat in KNOWN_CATEGORIES:
        if cat in goal_lower:
            found.append(cat)
    # Keyword synonyms
    synonyms = {
        "test": "testing", "fix": "debugging", "bug": "debugging",
        "perf": "performance", "optim": "performance",
        "doc": "documentation", "arch": "architecture",
        "secur": "security", "monitor": "monitoring",
        "refactor": "refactoring", "deploy": "deployment",
        "daemon": "infrastructure", "implement": "backend",
        "analys": "research", "audit": "code_review",
        "review": "code_review", "frontend": "frontend",
        "wire": "wiring", "dispatch": "wiring",
    }
    for keyword, cat in synonyms.items():
        if keyword in goal_lower and cat not in found:
            found.append(cat)
    return found or [DEFAULT_CATEGORY]


# ── Formation Strategies ─────────────────────────────────────────

def _form_skill_based(goal: str, size: int,
                      categories: list[str]) -> list[str]:
    """Select workers by specialization match.

    Uses skynet_specialization.recommend_worker() to rank workers for
    each category in the goal, then picks the top-N unique workers.
    """
    try:
        from tools.skynet_specialization import recommend_worker
    except ImportError:
        return WORKER_NAMES[:size]

    scored: dict[str, float] = {w: 0.0 for w in WORKER_NAMES}
    for cat in categories:
        rankings = recommend_worker(cat)
        for entry in rankings:
            w = entry.get("worker", "")
            if w in scored:
                scored[w] += entry.get("composite_score", 0.0)

    ranked = sorted(scored.items(), key=lambda x: x[1], reverse=True)
    selected = [w for w, _ in ranked[:size]]
    # If not enough scored workers, pad with remaining
    if len(selected) < size:
        remaining = [w for w in WORKER_NAMES if w not in selected]
        selected.extend(remaining[: size - len(selected)])
    return selected[:size]


def _form_load_based(goal: str, size: int,
                     categories: list[str]) -> list[str]:
    """Select workers by current load — prefer IDLE workers."""
    states = _get_worker_states()
    idle = [w for w, s in states.items() if s == "IDLE"]
    busy = [w for w in WORKER_NAMES if w not in idle]

    selected = idle[:size]
    if len(selected) < size:
        selected.extend(busy[: size - len(selected)])
    return selected[:size]


def _form_affinity_based(goal: str, size: int,
                         categories: list[str]) -> list[str]:
    """Select workers by historical collaboration success.

    Reads affinity scores from coalition history — workers that have
    been in successful coalitions together get higher affinity.
    """
    state = _load_state()
    affinity = state.get("affinity", {})

    # Build pairwise affinity matrix
    pair_scores: dict[str, float] = {}
    for key, score in affinity.items():
        pair_scores[key] = score

    # Score each worker by sum of affinities with all others
    scored: dict[str, float] = {w: 0.0 for w in WORKER_NAMES}
    for w in WORKER_NAMES:
        for other in WORKER_NAMES:
            if w == other:
                continue
            key = _affinity_key(w, other)
            scored[w] += pair_scores.get(key, 0.0)

    # Also incorporate skill relevance (30% weight)
    try:
        from tools.skynet_specialization import recommend_worker
        for cat in categories:
            rankings = recommend_worker(cat)
            for entry in rankings:
                w = entry.get("worker", "")
                if w in scored:
                    scored[w] += entry.get("composite_score", 0.0) * 0.3
    except ImportError:
        pass

    ranked = sorted(scored.items(), key=lambda x: x[1], reverse=True)
    selected = [w for w, _ in ranked[:size]]
    if len(selected) < size:
        remaining = [w for w in WORKER_NAMES if w not in selected]
        selected.extend(remaining[: size - len(selected)])
    return selected[:size]


def _affinity_key(w1: str, w2: str) -> str:
    """Canonical pairwise affinity key (alphabetical order)."""
    pair = sorted([w1, w2])
    return f"{pair[0]}:{pair[1]}"


def _update_affinity(state: dict, members: list[str],
                     success: bool) -> None:
    """Update pairwise affinity scores after coalition dissolution."""
    affinity = state.setdefault("affinity", {})
    delta = 0.1 if success else -0.05
    for i, w1 in enumerate(members):
        for w2 in members[i + 1:]:
            key = _affinity_key(w1, w2)
            current = affinity.get(key, 0.0)
            affinity[key] = max(-1.0, min(1.0, current + delta))


STRATEGY_MAP = {
    FormationStrategy.SKILL_BASED: _form_skill_based,
    FormationStrategy.LOAD_BASED: _form_load_based,
    FormationStrategy.AFFINITY_BASED: _form_affinity_based,
}


# ── Coalition Manager ────────────────────────────────────────────

class CoalitionManager:
    """Manages the lifecycle of worker coalitions.

    Coalitions are temporary squads formed for a specific goal.
    Members communicate via the bus using a shared coalition_id
    metadata tag. Coalitions auto-dissolve on completion or expiry.
    """

    def __init__(self) -> None:
        self.state = _load_state()

    def _save(self) -> None:
        _save_state(self.state)

    # ── Active coalitions ────────────────────────────────────────

    @property
    def active_coalitions(self) -> dict[str, Coalition]:
        return {
            cid: Coalition.from_dict(cd)
            for cid, cd in self.state.get("active", {}).items()
        }

    def get_coalition(self, coalition_id: str) -> Optional[Coalition]:
        raw = self.state.get("active", {}).get(coalition_id)
        if raw:
            return Coalition.from_dict(raw)
        # Check history
        for entry in self.state.get("history", []):
            if entry.get("coalition_id") == coalition_id:
                return Coalition.from_dict(entry)
        return None

    # ── Formation ────────────────────────────────────────────────

    def propose_coalition(
        self,
        goal: str,
        strategy: FormationStrategy = FormationStrategy.SKILL_BASED,
        size: int = DEFAULT_COALITION_SIZE,
        proposer: str = "orchestrator",
    ) -> Coalition:
        """Create a new coalition proposal.

        Selects members using the chosen formation strategy, assigns a
        leader (highest-scoring member), and transitions to ACTIVE.
        """
        size = max(1, min(size, MAX_COALITION_SIZE))
        categories = _extract_categories(goal)
        form_fn = STRATEGY_MAP[strategy]
        selected = form_fn(goal, size, categories)

        coalition_id = _generate_id(goal)
        coalition = Coalition(
            coalition_id=coalition_id,
            goal=goal,
            strategy=strategy,
            status=CoalitionStatus.ACTIVE,
            leader=selected[0] if selected else "",
            created_at=time.time(),
            activated_at=time.time(),
            categories=categories,
            max_size=size,
        )
        for i, worker in enumerate(selected):
            coalition.add_member(
                worker, role="leader" if i == 0 else "member"
            )

        self.state.setdefault("active", {})[coalition_id] = coalition.to_dict()
        self._save()

        # Announce on bus
        _bus_publish({
            "sender": proposer,
            "topic": "workers",
            "type": "coalition_propose",
            "content": (
                f"Coalition formed: {coalition_id}. "
                f"Goal: {goal}. Members: {', '.join(selected)}. "
                f"Strategy: {strategy.value}. Leader: {coalition.leader}."
            ),
            "metadata": {"coalition_id": coalition_id},
        })

        return coalition

    def join_coalition(self, coalition_id: str,
                       worker: str) -> tuple[bool, str]:
        """Worker joins an existing coalition."""
        raw = self.state.get("active", {}).get(coalition_id)
        if not raw:
            return False, "Coalition not found or not active"

        coalition = Coalition.from_dict(raw)
        if coalition.status != CoalitionStatus.ACTIVE:
            return False, f"Coalition is {coalition.status.value}"

        if not coalition.add_member(worker):
            if worker in coalition.member_names:
                return False, "Already a member"
            return False, "Coalition is full"

        self.state["active"][coalition_id] = coalition.to_dict()
        self._save()

        _bus_publish({
            "sender": worker,
            "topic": "workers",
            "type": "coalition_join",
            "content": f"{worker} joined coalition {coalition_id}.",
            "metadata": {"coalition_id": coalition_id},
        })
        return True, "Joined"

    def leave_coalition(self, coalition_id: str,
                        worker: str) -> tuple[bool, str]:
        """Worker leaves a coalition."""
        raw = self.state.get("active", {}).get(coalition_id)
        if not raw:
            return False, "Coalition not found"

        coalition = Coalition.from_dict(raw)
        if not coalition.remove_member(worker):
            return False, "Not a member"

        # If leader left, reassign
        if coalition.leader == worker and coalition.members:
            coalition.leader = coalition.members[0].worker
            coalition.members[0].role = "leader"

        # Auto-dissolve if empty
        if not coalition.members:
            return self.dissolve_coalition(
                coalition_id, reason="All members left"
            )

        self.state["active"][coalition_id] = coalition.to_dict()
        self._save()

        _bus_publish({
            "sender": worker,
            "topic": "workers",
            "type": "coalition_leave",
            "content": f"{worker} left coalition {coalition_id}.",
            "metadata": {"coalition_id": coalition_id},
        })
        return True, "Left"

    def dissolve_coalition(
        self, coalition_id: str,
        reason: str = "Goal achieved",
        success: bool = True,
    ) -> tuple[bool, str]:
        """Dissolve a coalition and archive it."""
        raw = self.state.get("active", {}).get(coalition_id)
        if not raw:
            return False, "Coalition not found"

        coalition = Coalition.from_dict(raw)
        coalition.status = (CoalitionStatus.COMPLETED if success
                            else CoalitionStatus.DISSOLVED)
        coalition.dissolved_at = time.time()
        coalition.dissolve_reason = reason

        # Update affinity scores
        member_names = coalition.member_names
        _update_affinity(self.state, member_names, success)

        # Move to history
        history = self.state.setdefault("history", [])
        history.append(coalition.to_dict())
        if len(history) > MAX_HISTORY:
            self.state["history"] = history[-MAX_HISTORY:]

        del self.state["active"][coalition_id]
        self._save()

        _bus_publish({
            "sender": coalition.leader or "system",
            "topic": "workers",
            "type": "coalition_dissolve",
            "content": (
                f"Coalition {coalition_id} dissolved. "
                f"Reason: {reason}. Members: {', '.join(member_names)}. "
                f"Success: {success}."
            ),
            "metadata": {"coalition_id": coalition_id},
        })
        return True, f"Dissolved ({coalition.status.value})"

    # ── Shared Workspace ─────────────────────────────────────────

    def share_result(self, coalition_id: str, worker: str,
                     content: str) -> tuple[bool, str]:
        """Share an intermediate result within the coalition."""
        raw = self.state.get("active", {}).get(coalition_id)
        if not raw:
            return False, "Coalition not found"

        coalition = Coalition.from_dict(raw)
        if worker not in coalition.member_names:
            return False, "Not a member of this coalition"

        entry = {
            "worker": worker,
            "content": content[:2000],
            "timestamp": time.time(),
        }
        coalition.shared_results.append(entry)
        coalition.record_contribution(worker)

        self.state["active"][coalition_id] = coalition.to_dict()
        self._save()

        _bus_publish({
            "sender": worker,
            "topic": "workers",
            "type": "coalition_share",
            "content": (
                f"[Coalition {coalition_id}] {worker}: "
                f"{content[:200]}"
            ),
            "metadata": {"coalition_id": coalition_id},
        })
        return True, "Shared"

    def read_shared(self, coalition_id: str,
                    limit: int = 20) -> list[dict]:
        """Read shared results from a coalition."""
        coalition = self.get_coalition(coalition_id)
        if not coalition:
            return []
        return coalition.shared_results[-limit:]

    # ── Auto-Dissolve Expired ────────────────────────────────────

    def expire_stale(self) -> list[str]:
        """Dissolve coalitions that have exceeded their time limit."""
        expired: list[str] = []
        for cid in list(self.state.get("active", {}).keys()):
            coalition = Coalition.from_dict(self.state["active"][cid])
            if coalition.is_expired:
                self.dissolve_coalition(
                    cid, reason="Expired (time limit)", success=False
                )
                expired.append(cid)
        return expired

    # ── Query ────────────────────────────────────────────────────

    def worker_coalitions(self, worker: str) -> list[Coalition]:
        """Return all active coalitions a worker belongs to."""
        result: list[Coalition] = []
        for cid, raw in self.state.get("active", {}).items():
            c = Coalition.from_dict(raw)
            if worker in c.member_names:
                result.append(c)
        return result

    def coalition_history(self, limit: int = 20) -> list[dict]:
        """Return recent coalition history."""
        history = self.state.get("history", [])
        return history[-limit:]

    def affinity_matrix(self) -> dict[str, float]:
        """Return the pairwise affinity scores."""
        return dict(self.state.get("affinity", {}))

    def status_summary(self, coalition_id: Optional[str] = None) -> str:
        """Human-readable status summary."""
        lines: list[str] = []
        if coalition_id:
            coalition = self.get_coalition(coalition_id)
            if not coalition:
                return f"Coalition {coalition_id} not found."
            lines.append(_format_coalition(coalition, verbose=True))
        else:
            active = self.active_coalitions
            if not active:
                lines.append("No active coalitions.")
            else:
                lines.append(f"Active Coalitions: {len(active)}")
                lines.append("-" * 70)
                for cid, c in active.items():
                    lines.append(_format_coalition(c))
                    lines.append("")

            # Affinity summary
            aff = self.affinity_matrix()
            if aff:
                lines.append("\nAffinity Scores:")
                for key in sorted(aff, key=lambda k: aff[k],
                                  reverse=True):
                    lines.append(f"  {key}: {aff[key]:+.2f}")
        return "\n".join(lines)


def _format_coalition(c: Coalition, verbose: bool = False) -> str:
    """Format a coalition for display."""
    members_str = ", ".join(
        f"{m.worker}({'L' if m.role == 'leader' else 'M'})"
        for m in c.members
    )
    age = _format_duration(c.age_s)
    line = (
        f"[{c.coalition_id}] {c.status.value.upper():10s} "
        f"strategy={c.strategy.value:8s} "
        f"members=[{members_str}] age={age}"
    )
    if verbose:
        parts = [line]
        parts.append(f"  Goal: {c.goal}")
        parts.append(f"  Categories: {', '.join(c.categories)}")
        parts.append(f"  Leader: {c.leader}")
        parts.append(f"  Created: {_ts(c.created_at)}")
        if c.dissolved_at:
            parts.append(f"  Dissolved: {_ts(c.dissolved_at)}")
            parts.append(f"  Reason: {c.dissolve_reason}")
        parts.append(f"  Shared results: {len(c.shared_results)}")
        for m in c.members:
            parts.append(
                f"    {m.worker}: contributions={m.contributions}, "
                f"role={m.role}"
            )
        return "\n".join(parts)
    return line


def _format_duration(seconds: float) -> str:
    """Human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _ts(epoch: float) -> str:
    """Epoch to ISO-ish string."""
    if epoch <= 0:
        return "-"
    import datetime as _dt
    return _dt.datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


# ── CLI ──────────────────────────────────────────────────────────

def _cli() -> None:
    """Command-line interface for coalition management."""
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    parser = argparse.ArgumentParser(
        description="Skynet Coalition Formation (Squad Mode)")
    sub = parser.add_subparsers(dest="command")

    # form
    p_form = sub.add_parser("form", help="Form a new coalition")
    p_form.add_argument("--goal", required=True, help="Coalition goal")
    p_form.add_argument("--strategy", default="skill",
                        choices=["skill", "load", "affinity"],
                        help="Formation strategy")
    p_form.add_argument("--size", type=int, default=DEFAULT_COALITION_SIZE,
                        help="Number of members")
    p_form.add_argument("--proposer", default="orchestrator",
                        help="Who is proposing")

    # status
    p_status = sub.add_parser("status", help="Show coalition status")
    p_status.add_argument("--id", default=None, help="Coalition ID")

    # dissolve
    p_dissolve = sub.add_parser("dissolve", help="Dissolve a coalition")
    p_dissolve.add_argument("--id", required=True, help="Coalition ID")
    p_dissolve.add_argument("--reason", default="Goal achieved")
    p_dissolve.add_argument("--failed", action="store_true",
                            help="Mark as failed")

    # history
    p_hist = sub.add_parser("history", help="Show coalition history")
    p_hist.add_argument("--limit", type=int, default=10)

    # share
    p_share = sub.add_parser("share",
                             help="Share result within coalition")
    p_share.add_argument("--id", required=True, help="Coalition ID")
    p_share.add_argument("--content", required=True, help="Result text")
    p_share.add_argument("--sender", required=True, help="Worker name")

    # read
    p_read = sub.add_parser("read",
                            help="Read shared results from coalition")
    p_read.add_argument("--id", required=True, help="Coalition ID")
    p_read.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    mgr = CoalitionManager()

    if args.command == "form":
        strategy = {
            "skill": FormationStrategy.SKILL_BASED,
            "load": FormationStrategy.LOAD_BASED,
            "affinity": FormationStrategy.AFFINITY_BASED,
        }[args.strategy]
        coal = mgr.propose_coalition(
            goal=args.goal, strategy=strategy,
            size=args.size, proposer=args.proposer,
        )
        print(f"Coalition formed: {coal.coalition_id}")
        print(f"  Goal: {coal.goal}")
        print(f"  Strategy: {coal.strategy.value}")
        print(f"  Members: {', '.join(coal.member_names)}")
        print(f"  Leader: {coal.leader}")
        print(f"  Categories: {', '.join(coal.categories)}")

    elif args.command == "status":
        # Expire stale coalitions first
        expired = mgr.expire_stale()
        if expired:
            print(f"Auto-expired: {', '.join(expired)}\n")
        print(mgr.status_summary(args.id))

    elif args.command == "dissolve":
        ok, msg = mgr.dissolve_coalition(
            args.id, reason=args.reason,
            success=not args.failed,
        )
        print(f"{'OK' if ok else 'FAILED'}: {msg}")

    elif args.command == "history":
        history = mgr.coalition_history(limit=args.limit)
        if not history:
            print("No coalition history.")
        else:
            print(f"Coalition History (last {args.limit}):")
            print("-" * 70)
            for entry in history:
                c = Coalition.from_dict(entry)
                members = ", ".join(c.member_names)
                duration = _format_duration(
                    c.dissolved_at - c.created_at
                ) if c.dissolved_at else "-"
                print(
                    f"  [{c.coalition_id}] {c.status.value:10s} "
                    f"goal='{c.goal[:40]}' "
                    f"members=[{members}] "
                    f"duration={duration}"
                )

    elif args.command == "share":
        ok, msg = mgr.share_result(args.id, args.sender, args.content)
        print(f"{'OK' if ok else 'FAILED'}: {msg}")

    elif args.command == "read":
        results = mgr.read_shared(args.id, limit=args.limit)
        if not results:
            print("No shared results.")
        else:
            print(f"Shared Results ({len(results)}):")
            for r in results:
                ts = _ts(r.get("timestamp", 0))
                print(f"  [{ts}] {r['worker']}: {r['content'][:100]}")


if __name__ == "__main__":
    _cli()
# signed: gamma
