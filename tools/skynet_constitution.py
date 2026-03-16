"""Skynet Constitutional AI Governance (P3.09).

Every agent action can be validated against a set of constitutional
principles.  Each principle has a check function, severity, and
enforcement mode.  Amendments follow a 3-of-5 majority vote protocol
published on the Skynet bus.

Built-in principles encode the most critical Skynet rules:
  * no_process_kill — workers must never terminate processes
  * no_self_dispatch — workers must not dispatch to themselves
  * truth_principle — no fabricated data or false claims
  * no_unsigned_work — all code changes must be signed
  * spam_guard_required — bus publishes must use guarded_publish
  * no_direct_orchestrator_work — orchestrator delegates, never implements
  * model_guard — workers must run Claude Opus 4.6 fast

CLI
---
    python tools/skynet_constitution.py rules          # list all rules
    python tools/skynet_constitution.py validate ACTION [--context JSON]
    python tools/skynet_constitution.py propose  --rule NAME --action add|remove|amend --description TEXT --proposer WORKER
    python tools/skynet_constitution.py vote     --amendment-id ID --voter WORKER --approve
    python tools/skynet_constitution.py history  [--limit N]

State: data/constitution.json
"""
# signed: gamma
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

# ── Paths ────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONSTITUTION_PATH = DATA_DIR / "constitution.json"

# ── Constants ────────────────────────────────────────────────────
WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]
ALL_VOTERS = WORKER_NAMES + ["orchestrator"]
MAJORITY_THRESHOLD = 3          # 3 of 5 agents
AMENDMENT_EXPIRE_S = 3600       # 1 hour to gather votes
MAX_HISTORY = 200


# ── Enums ────────────────────────────────────────────────────────

class Severity(Enum):
    """How a violation is handled."""
    WARNING = "warning"     # log but allow
    BLOCK = "block"         # prevent action
    CRITICAL = "critical"   # prevent + alert orchestrator


class AmendmentAction(Enum):
    ADD = "add"
    REMOVE = "remove"
    AMEND = "amend"


class AmendmentStatus(Enum):
    PROPOSED = "proposed"
    RATIFIED = "ratified"
    REJECTED = "rejected"
    EXPIRED = "expired"


# ── Data Classes ─────────────────────────────────────────────────

@dataclass
class Principle:
    """A constitutional rule that governs agent behaviour."""
    name: str
    description: str
    severity: Severity = Severity.BLOCK
    enabled: bool = True
    # check_fn is registered separately in the check registry
    added_by: str = "genesis"
    added_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "severity": self.severity.value,
            "enabled": self.enabled,
            "added_by": self.added_by,
            "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Principle:
        return cls(
            name=d["name"],
            description=d.get("description", ""),
            severity=Severity(d.get("severity", "block")),
            enabled=d.get("enabled", True),
            added_by=d.get("added_by", "genesis"),
            added_at=d.get("added_at", 0.0),
        )


@dataclass
class Violation:
    """A detected violation of a principle."""
    principle: str
    severity: Severity
    message: str
    blocked: bool = False

    def to_dict(self) -> dict:
        return {
            "principle": self.principle,
            "severity": self.severity.value,
            "message": self.message,
            "blocked": self.blocked,
        }


@dataclass
class Amendment:
    """A proposed change to the constitution."""
    amendment_id: str
    rule_name: str
    action: AmendmentAction
    description: str
    proposer: str
    status: AmendmentStatus = AmendmentStatus.PROPOSED
    votes_yes: list[str] = field(default_factory=list)
    votes_no: list[str] = field(default_factory=list)
    created_at: float = 0.0
    resolved_at: float = 0.0
    # For "amend" action: new values
    new_severity: Optional[str] = None
    new_description: Optional[str] = None

    @property
    def total_votes(self) -> int:
        return len(self.votes_yes) + len(self.votes_no)

    @property
    def is_expired(self) -> bool:
        return (self.status == AmendmentStatus.PROPOSED
                and (time.time() - self.created_at) > AMENDMENT_EXPIRE_S)

    def check_majority(self) -> Optional[AmendmentStatus]:
        """Return RATIFIED/REJECTED if majority reached, else None."""
        if len(self.votes_yes) >= MAJORITY_THRESHOLD:
            return AmendmentStatus.RATIFIED
        if len(self.votes_no) >= MAJORITY_THRESHOLD:
            return AmendmentStatus.REJECTED
        return None

    def to_dict(self) -> dict:
        return {
            "amendment_id": self.amendment_id,
            "rule_name": self.rule_name,
            "action": self.action.value,
            "description": self.description,
            "proposer": self.proposer,
            "status": self.status.value,
            "votes_yes": self.votes_yes,
            "votes_no": self.votes_no,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "new_severity": self.new_severity,
            "new_description": self.new_description,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Amendment:
        return cls(
            amendment_id=d["amendment_id"],
            rule_name=d["rule_name"],
            action=AmendmentAction(d.get("action", "add")),
            description=d.get("description", ""),
            proposer=d.get("proposer", ""),
            status=AmendmentStatus(d.get("status", "proposed")),
            votes_yes=d.get("votes_yes", []),
            votes_no=d.get("votes_no", []),
            created_at=d.get("created_at", 0.0),
            resolved_at=d.get("resolved_at", 0.0),
            new_severity=d.get("new_severity"),
            new_description=d.get("new_description"),
        )


# ── Check Functions (Principle Implementations) ──────────────────

# Each check function takes (action: str, context: dict) -> Optional[str]
# Returns None if OK, or a violation message string.

def _check_no_process_kill(action: str, context: dict) -> Optional[str]:
    """Workers must never terminate processes."""
    kill_patterns = [
        r"\bStop-Process\b", r"\btaskkill\b", r"\bkill\(\)",
        r"\bterminate\(\)", r"\bos\.kill\b",
        r"Get-Process.*\|\s*Stop-Process",
    ]
    for pat in kill_patterns:
        if re.search(pat, action, re.IGNORECASE):
            return f"Process termination detected: matches '{pat}'"
    return None


def _check_no_self_dispatch(action: str, context: dict) -> Optional[str]:
    """Workers must not dispatch tasks to themselves."""
    sender = context.get("sender", "")
    target = context.get("target", "")
    if sender and target and sender == target and sender in WORKER_NAMES:
        return f"Self-dispatch detected: {sender} -> {target}"
    # Also check action text
    if re.search(r"dispatch.*to\s+(my)?self", action, re.IGNORECASE):
        return "Self-dispatch pattern detected in action text"
    return None


def _check_truth_principle(action: str, context: dict) -> Optional[str]:
    """No fabricated data, fake metrics, or false claims."""
    fabrication_patterns = [
        r"fake\s+(data|metric|result|count|status)",
        r"fabricat(e|ed|ing)\s+(data|metric|result)",
        r"placeholder.*disguised\s+as\s+real",
        r"simulated\s+activity",
        r"synthetic\s+filler",
    ]
    for pat in fabrication_patterns:
        if re.search(pat, action, re.IGNORECASE):
            return f"Truth violation: matches fabrication pattern '{pat}'"
    return None


def _check_no_unsigned_work(action: str, context: dict) -> Optional[str]:
    """All code changes must be signed."""
    # Only applies when context indicates a code change
    if context.get("is_code_change", False):
        content = context.get("content", action)
        if "signed:" not in content and "# signed:" not in content:
            return "Code change without signature (missing '# signed: worker')"
    return None


def _check_spam_guard_required(action: str, context: dict) -> Optional[str]:
    """Bus publishes must use guarded_publish, not raw requests.post."""
    spam_patterns = [
        r"requests\.post\(.*/bus/publish",
        r"urllib.*request.*bus/publish",
        r"POST.*localhost.*8420/bus/publish",
    ]
    for pat in spam_patterns:
        if re.search(pat, action, re.IGNORECASE):
            return (
                "Raw bus publish detected. Must use guarded_publish() "
                "from tools.skynet_spam_guard"
            )
    return None


def _check_no_direct_orch_work(action: str, context: dict) -> Optional[str]:
    """Orchestrator must delegate, never implement directly."""
    sender = context.get("sender", "")
    if sender != "orchestrator":
        return None
    direct_patterns = [
        r"edit\s+tool.*file",
        r"directly\s+(edit|modify|change|fix)",
        r"I'll\s+(fix|edit|change|modify)\s+(the|this)\s+file",
    ]
    for pat in direct_patterns:
        if re.search(pat, action, re.IGNORECASE):
            return "Orchestrator attempting direct implementation work"
    return None


def _check_model_guard(action: str, context: dict) -> Optional[str]:
    """Workers must run Claude Opus 4.6 fast mode."""
    model = context.get("model", "")
    if model and "opus" not in model.lower() and "fast" not in model.lower():
        return f"Model guard violation: worker using '{model}' instead of Opus 4.6 fast"
    return None


# ── Check Registry ───────────────────────────────────────────────

# Maps principle name -> check function
CHECK_REGISTRY: dict[str, Callable] = {
    "no_process_kill": _check_no_process_kill,
    "no_self_dispatch": _check_no_self_dispatch,
    "truth_principle": _check_truth_principle,
    "no_unsigned_work": _check_no_unsigned_work,
    "spam_guard_required": _check_spam_guard_required,
    "no_direct_orchestrator_work": _check_no_direct_orch_work,
    "model_guard": _check_model_guard,
}

# Default principle definitions
DEFAULT_PRINCIPLES: list[dict] = [
    {
        "name": "no_process_kill",
        "description": (
            "Workers must NEVER execute Stop-Process, taskkill, kill(), "
            "terminate(), or any process termination command."
        ),
        "severity": "critical",
        "added_by": "genesis",
    },
    {
        "name": "no_self_dispatch",
        "description": (
            "Workers must never dispatch tasks to themselves. "
            "Self-dispatch creates infinite loops (INCIDENT 001)."
        ),
        "severity": "block",
        "added_by": "genesis",
    },
    {
        "name": "truth_principle",
        "description": (
            "Every piece of data displayed, every metric shown, every "
            "status reported must reflect REALITY. No fabrication, "
            "no decoration, no placeholder data disguised as real."
        ),
        "severity": "critical",
        "added_by": "genesis",
    },
    {
        "name": "no_unsigned_work",
        "description": (
            "All code changes must include a signature comment "
            "(# signed: worker_name) near the changed code."
        ),
        "severity": "warning",
        "added_by": "genesis",
    },
    {
        "name": "spam_guard_required",
        "description": (
            "All bus publishes must use guarded_publish() from "
            "tools.skynet_spam_guard. Raw requests.post to /bus/publish "
            "is FORBIDDEN and costs -1.0 score."
        ),
        "severity": "block",
        "added_by": "genesis",
    },
    {
        "name": "no_direct_orchestrator_work",
        "description": (
            "The orchestrator must delegate ALL implementation work to "
            "workers. It must never edit files, run scripts, or execute "
            "commands directly."
        ),
        "severity": "block",
        "added_by": "genesis",
    },
    {
        "name": "model_guard",
        "description": (
            "All workers and orchestrator must run Claude Opus 4.6 "
            "(fast mode) with Copilot CLI agent at all times."
        ),
        "severity": "critical",
        "added_by": "genesis",
    },
]


# ── State Persistence ────────────────────────────────────────────

def _load_state() -> dict:
    """Load constitution state from disk."""
    if CONSTITUTION_PATH.exists():
        try:
            with open(CONSTITUTION_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return _init_state()


def _init_state() -> dict:
    """Create initial constitution state with default principles."""
    return {
        "principles": {p["name"]: p for p in DEFAULT_PRINCIPLES},
        "amendments": {},
        "history": [],
        "stats": {
            "total_validations": 0,
            "total_violations": 0,
            "total_blocked": 0,
            "violations_by_principle": {},
        },
        "version": 1,
    }


def _save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONSTITUTION_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(CONSTITUTION_PATH)


def _bus_publish(msg: dict) -> bool:
    try:
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish(msg)
        return result.get("published", False)
    except Exception:
        return False


# ── Constitutional Governor ──────────────────────────────────────

class ConstitutionalGovernor:
    """Validates agent actions against constitutional principles.

    Every action can be checked against the full set of enabled
    principles.  Violations are logged and, depending on severity,
    may block the action entirely.
    """

    def __init__(self) -> None:
        self.state = _load_state()

    def _save(self) -> None:
        _save_state(self.state)

    # ── Principle Access ─────────────────────────────────────────

    @property
    def principles(self) -> dict[str, Principle]:
        return {
            name: Principle.from_dict(pd)
            for name, pd in self.state.get("principles", {}).items()
        }

    def get_principle(self, name: str) -> Optional[Principle]:
        pd = self.state.get("principles", {}).get(name)
        return Principle.from_dict(pd) if pd else None

    # ── Validation ───────────────────────────────────────────────

    def validate_action(
        self, action: str, context: Optional[dict] = None,
    ) -> tuple[bool, list[Violation]]:
        """Check an action against all enabled principles.

        Args:
            action:  Description of the action being taken.
            context: Optional dict with sender, target, model, etc.

        Returns:
            (allowed, violations) — allowed is False if any BLOCK/CRITICAL
            principle is violated.
        """
        context = context or {}
        violations: list[Violation] = []
        allowed = True

        for name, principle in self.principles.items():
            if not principle.enabled:
                continue
            check_fn = CHECK_REGISTRY.get(name)
            if not check_fn:
                continue

            result = check_fn(action, context)
            if result is not None:
                blocked = principle.severity in (
                    Severity.BLOCK, Severity.CRITICAL
                )
                violations.append(Violation(
                    principle=name,
                    severity=principle.severity,
                    message=result,
                    blocked=blocked,
                ))
                if blocked:
                    allowed = False

        # Update stats
        stats = self.state.setdefault("stats", {})
        stats["total_validations"] = stats.get("total_validations", 0) + 1
        if violations:
            stats["total_violations"] = (
                stats.get("total_violations", 0) + len(violations)
            )
            if not allowed:
                stats["total_blocked"] = stats.get("total_blocked", 0) + 1
            by_p = stats.setdefault("violations_by_principle", {})
            for v in violations:
                by_p[v.principle] = by_p.get(v.principle, 0) + 1

        self._save()
        return allowed, violations

    # ── Amendment Protocol ───────────────────────────────────────

    def propose_amendment(
        self,
        rule_name: str,
        action: AmendmentAction,
        description: str,
        proposer: str,
        new_severity: Optional[str] = None,
        new_description: Optional[str] = None,
    ) -> Amendment:
        """Propose a constitutional amendment.

        Requires 3-of-5 majority vote (workers + orchestrator) to pass.
        """
        if action == AmendmentAction.REMOVE:
            if rule_name not in self.state.get("principles", {}):
                raise ValueError(f"Principle '{rule_name}' not found")

        aid = "amd_" + hashlib.sha256(
            f"{rule_name}:{action.value}:{time.time()}".encode()
        ).hexdigest()[:12]

        amendment = Amendment(
            amendment_id=aid,
            rule_name=rule_name,
            action=action,
            description=description,
            proposer=proposer,
            created_at=time.time(),
            new_severity=new_severity,
            new_description=new_description,
        )
        # Proposer auto-votes yes
        amendment.votes_yes.append(proposer)

        self.state.setdefault("amendments", {})[aid] = amendment.to_dict()
        self._save()

        _bus_publish({
            "sender": proposer,
            "topic": "planning",
            "type": "proposal",
            "content": (
                f"Constitutional amendment proposed: {aid}. "
                f"Action: {action.value} rule '{rule_name}'. "
                f"{description}. Vote with: python tools/skynet_constitution.py "
                f"vote --amendment-id {aid} --voter YOURNAME --approve"
            ),
            "metadata": {"amendment_id": aid},
        })

        return amendment

    def vote_amendment(
        self, amendment_id: str, voter: str, approve: bool,
    ) -> tuple[str, Optional[str]]:
        """Cast a vote on a pending amendment.

        Returns:
            (status_message, new_status_or_None)
        """
        raw = self.state.get("amendments", {}).get(amendment_id)
        if not raw:
            return "Amendment not found", None

        amendment = Amendment.from_dict(raw)

        if amendment.status != AmendmentStatus.PROPOSED:
            return f"Amendment already {amendment.status.value}", None

        if amendment.is_expired:
            amendment.status = AmendmentStatus.EXPIRED
            amendment.resolved_at = time.time()
            self.state["amendments"][amendment_id] = amendment.to_dict()
            self._archive_amendment(amendment)
            self._save()
            return "Amendment expired", "expired"

        if voter not in ALL_VOTERS:
            return f"Invalid voter '{voter}'", None

        all_voted = amendment.votes_yes + amendment.votes_no
        if voter in all_voted:
            return f"{voter} already voted", None

        if approve:
            amendment.votes_yes.append(voter)
        else:
            amendment.votes_no.append(voter)

        # Check for majority
        result = amendment.check_majority()
        if result == AmendmentStatus.RATIFIED:
            amendment.status = AmendmentStatus.RATIFIED
            amendment.resolved_at = time.time()
            self._apply_amendment(amendment)
            msg = f"Amendment RATIFIED ({len(amendment.votes_yes)} yes)"
        elif result == AmendmentStatus.REJECTED:
            amendment.status = AmendmentStatus.REJECTED
            amendment.resolved_at = time.time()
            msg = f"Amendment REJECTED ({len(amendment.votes_no)} no)"
        else:
            msg = (
                f"Vote recorded. "
                f"Yes: {len(amendment.votes_yes)}, "
                f"No: {len(amendment.votes_no)}, "
                f"Need: {MAJORITY_THRESHOLD}"
            )

        self.state["amendments"][amendment_id] = amendment.to_dict()
        if amendment.status != AmendmentStatus.PROPOSED:
            self._archive_amendment(amendment)
        self._save()

        return msg, (amendment.status.value
                     if amendment.status != AmendmentStatus.PROPOSED
                     else None)

    def _apply_amendment(self, amendment: Amendment) -> None:
        """Apply a ratified amendment to the constitution."""
        principles = self.state.setdefault("principles", {})

        if amendment.action == AmendmentAction.ADD:
            principles[amendment.rule_name] = {
                "name": amendment.rule_name,
                "description": amendment.description,
                "severity": amendment.new_severity or "block",
                "enabled": True,
                "added_by": amendment.proposer,
                "added_at": time.time(),
            }

        elif amendment.action == AmendmentAction.REMOVE:
            principles.pop(amendment.rule_name, None)

        elif amendment.action == AmendmentAction.AMEND:
            existing = principles.get(amendment.rule_name, {})
            if amendment.new_severity:
                existing["severity"] = amendment.new_severity
            if amendment.new_description:
                existing["description"] = amendment.new_description
            principles[amendment.rule_name] = existing

    def _archive_amendment(self, amendment: Amendment) -> None:
        """Move resolved amendment to history."""
        history = self.state.setdefault("history", [])
        history.append(amendment.to_dict())
        if len(history) > MAX_HISTORY:
            self.state["history"] = history[-MAX_HISTORY:]
        # Remove from active
        self.state.get("amendments", {}).pop(amendment.amendment_id, None)

    def expire_stale_amendments(self) -> list[str]:
        """Expire amendments that exceeded the time limit."""
        expired: list[str] = []
        for aid in list(self.state.get("amendments", {}).keys()):
            amd = Amendment.from_dict(self.state["amendments"][aid])
            if amd.is_expired:
                amd.status = AmendmentStatus.EXPIRED
                amd.resolved_at = time.time()
                self.state["amendments"][aid] = amd.to_dict()
                self._archive_amendment(amd)
                expired.append(aid)
        if expired:
            self._save()
        return expired

    # ── Queries ──────────────────────────────────────────────────

    def pending_amendments(self) -> list[Amendment]:
        return [
            Amendment.from_dict(d)
            for d in self.state.get("amendments", {}).values()
            if d.get("status") == "proposed"
        ]

    def amendment_history(self, limit: int = 20) -> list[dict]:
        return self.state.get("history", [])[-limit:]

    def rules_summary(self) -> str:
        """Human-readable rules listing."""
        lines = ["Constitutional Principles", "=" * 50]
        for name, p in self.principles.items():
            status = "ENABLED" if p.enabled else "DISABLED"
            has_check = name in CHECK_REGISTRY
            lines.append(
                f"\n  [{p.severity.value.upper():8s}] {name}"
            )
            lines.append(f"    Status: {status} | Check: {'yes' if has_check else 'no'}")
            lines.append(f"    {p.description}")
        stats = self.state.get("stats", {})
        lines.append(f"\nStats: {stats.get('total_validations', 0)} validations, "
                     f"{stats.get('total_violations', 0)} violations, "
                     f"{stats.get('total_blocked', 0)} blocked")
        return "\n".join(lines)

    def validation_stats(self) -> dict:
        return dict(self.state.get("stats", {}))


# ── CLI ──────────────────────────────────────────────────────────

def _cli() -> None:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    parser = argparse.ArgumentParser(
        description="Skynet Constitutional AI Governance")
    sub = parser.add_subparsers(dest="command")

    # rules
    sub.add_parser("rules", help="List all constitutional principles")

    # validate
    p_val = sub.add_parser("validate", help="Validate an action")
    p_val.add_argument("action", help="Action description to validate")
    p_val.add_argument("--context", default="{}",
                       help="JSON context (sender, target, model, etc)")

    # propose
    p_prop = sub.add_parser("propose", help="Propose an amendment")
    p_prop.add_argument("--rule", required=True, help="Rule name")
    p_prop.add_argument("--action", required=True,
                        choices=["add", "remove", "amend"])
    p_prop.add_argument("--description", required=True)
    p_prop.add_argument("--proposer", required=True)
    p_prop.add_argument("--new-severity",
                        choices=["warning", "block", "critical"])
    p_prop.add_argument("--new-description", default=None)

    # vote
    p_vote = sub.add_parser("vote", help="Vote on an amendment")
    p_vote.add_argument("--amendment-id", required=True)
    p_vote.add_argument("--voter", required=True)
    p_vote.add_argument("--approve", action="store_true", default=False)
    p_vote.add_argument("--reject", action="store_true", default=False)

    # history
    p_hist = sub.add_parser("history", help="Amendment history")
    p_hist.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    gov = ConstitutionalGovernor()

    if args.command == "rules":
        print(gov.rules_summary())

    elif args.command == "validate":
        try:
            ctx = json.loads(args.context)
        except json.JSONDecodeError:
            ctx = {}
        allowed, violations = gov.validate_action(args.action, ctx)
        if not violations:
            print("ALLOWED: No violations detected.")
        else:
            print(f"{'ALLOWED' if allowed else 'BLOCKED'}: "
                  f"{len(violations)} violation(s)")
            for v in violations:
                marker = "!!" if v.blocked else "~~"
                print(f"  [{marker}] {v.severity.value.upper():8s} "
                      f"{v.principle}: {v.message}")

    elif args.command == "propose":
        amd = gov.propose_amendment(
            rule_name=args.rule,
            action=AmendmentAction(args.action),
            description=args.description,
            proposer=args.proposer,
            new_severity=getattr(args, "new_severity", None),
            new_description=getattr(args, "new_description", None),
        )
        print(f"Amendment proposed: {amd.amendment_id}")
        print(f"  Rule: {amd.rule_name}")
        print(f"  Action: {amd.action.value}")
        print(f"  Proposer: {amd.proposer} (auto-voted YES)")
        print(f"  Votes needed: {MAJORITY_THRESHOLD}")

    elif args.command == "vote":
        approve = args.approve
        if args.reject:
            approve = False
        msg, new_status = gov.vote_amendment(
            args.amendment_id, args.voter, approve
        )
        print(msg)
        if new_status:
            print(f"  Final status: {new_status}")

    elif args.command == "history":
        expired = gov.expire_stale_amendments()
        if expired:
            print(f"Auto-expired: {', '.join(expired)}\n")
        history = gov.amendment_history(limit=args.limit)
        if not history:
            print("No amendment history.")
        else:
            print(f"Amendment History (last {args.limit}):")
            print("-" * 60)
            for entry in history:
                amd = Amendment.from_dict(entry)
                yes = len(amd.votes_yes)
                no = len(amd.votes_no)
                print(
                    f"  [{amd.amendment_id}] {amd.status.value:10s} "
                    f"{amd.action.value:6s} '{amd.rule_name}' "
                    f"by {amd.proposer} "
                    f"(yes={yes}, no={no})"
                )


if __name__ == "__main__":
    _cli()
# signed: gamma
