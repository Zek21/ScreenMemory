#!/usr/bin/env python3
"""
skynet_convene.py -- Convene Session Helper for worker-side collaboration.
Enables workers to initiate, discover, join, and resolve multi-worker
coordination sessions via the Skynet bus.

Usage:
    python skynet_convene.py --initiate --topic "code review" --context "review auth" --worker alpha
    python skynet_convene.py --discover
    python skynet_convene.py --join SESSION_ID --worker beta
    python skynet_convene.py --status
    python skynet_convene.py --orchestrate "topic" --timeout 60
    python skynet_convene.py --vote SESSION_ID --proposal "use REST" --choice YES --worker alpha
    python skynet_convene.py --consensus SESSION_ID --proposal "use REST"
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SESSIONS_FILE = DATA / "convene_sessions.json"
WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

SKYNET = "http://localhost:8420"
BUS_PUBLISH = f"{SKYNET}/bus/publish"
BUS_MESSAGES = f"{SKYNET}/bus/messages"
BUS_CONVENE = f"{SKYNET}/bus/convene"


# ── Session Lifecycle ──────────────────────────────────────────────

def initiate_convene(worker_name: str, topic: str, context: str, need_workers: int = 2) -> Optional[str]:
    """POST /bus/convene to create a session. Returns session_id or None."""
    try:
        r = requests.post(BUS_CONVENE, json={
            "initiator": worker_name,
            "topic": topic,
            "context": context,
            "need_workers": need_workers,
        }, timeout=5)
        if r.ok:
            data = r.json()
            return data.get("session_id")
        print(f"[convene] initiate failed: HTTP {r.status_code}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"[convene] initiate error: {e}", file=sys.stderr)
    return None


def discover_sessions() -> List[Dict]:
    """GET /bus/convene to list active sessions."""
    try:
        r = requests.get(BUS_CONVENE, timeout=5)
        if r.ok:
            sessions = r.json()
            return sessions if isinstance(sessions, list) else []
    except requests.RequestException as e:
        print(f"[convene] discover error: {e}", file=sys.stderr)
    return []


def join_session(worker_name: str, session_id: str) -> bool:
    """Join a convene session via PATCH /bus/convene and notify via bus."""
    try:
        # Update session participants in Go backend
        requests.patch(BUS_CONVENE, json={
            "session_id": session_id, "worker": worker_name,
        }, timeout=5)
        # Also post bus message for visibility
        r = requests.post(BUS_PUBLISH, json={
            "sender": worker_name,
            "topic": "convene",
            "type": "join",
            "content": json.dumps({"session_id": session_id, "worker": worker_name}),
        }, timeout=5)
        return r.ok
    except requests.RequestException as e:
        print(f"[convene] join error: {e}", file=sys.stderr)
    return False


def post_update(worker_name: str, session_id: str, content: str) -> bool:
    """Post a work update to the convene session."""
    try:
        r = requests.post(BUS_PUBLISH, json={
            "sender": worker_name,
            "topic": "convene",
            "type": "update",
            "content": json.dumps({"session_id": session_id, "content": content}),
        }, timeout=5)
        return r.ok
    except requests.RequestException as e:
        print(f"[convene] update error: {e}", file=sys.stderr)
    return False


def resolve_session(worker_name: str, session_id: str, summary: str) -> bool:
    """Close a convene session via DELETE /bus/convene and post summary to bus."""
    try:
        # Mark session as resolved in Go backend
        requests.delete(f"{BUS_CONVENE}?id={session_id}", timeout=5)
        # Post summary to bus for visibility
        r = requests.post(BUS_PUBLISH, json={
            "sender": worker_name,
            "topic": "convene",
            "type": "resolve",
            "content": json.dumps({"session_id": session_id, "summary": summary}),
        }, timeout=5)
        return r.ok
    except requests.RequestException as e:
        print(f"[convene] resolve error: {e}", file=sys.stderr)
    return False


# ── Auto-Discovery ────────────────────────────────────────────────

def poll_and_join(worker_name: str, interests: Optional[List[str]] = None) -> List[str]:
    """Poll active sessions, auto-join any matching worker interests.
    Returns list of joined session_ids."""
    sessions = discover_sessions()
    joined = []
    for s in sessions:
        if s.get("status") != "active":
            continue
        # Skip if already a participant
        if worker_name in (s.get("participants") or []):
            continue
        # Check if session needs more workers
        participants = s.get("participants") or []
        if len(participants) >= (s.get("need_workers") or 2):
            continue
        # Match interests if provided
        if interests:
            topic = (s.get("topic") or "").lower()
            context = (s.get("context") or "").lower()
            match = any(kw.lower() in topic or kw.lower() in context for kw in interests)
            if not match:
                continue
        sid = s.get("id", "")
        if join_session(worker_name, sid):
            joined.append(sid)
            print(f"[convene] {worker_name} joined session {sid}: {s.get('topic')}")
    return joined


# ── Collaboration Protocol ────────────────────────────────────────

def collect_updates(session_id: str, timeout: float = 30, expected: int = 0) -> List[Dict]:
    """Poll bus for all type='update' messages matching session_id.
    Returns when expected count reached or timeout."""
    updates = []
    seen_ids = set()
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            r = requests.get(BUS_MESSAGES, params={"limit": 50, "topic": "convene"}, timeout=5)
            if r.ok:
                msgs = r.json() if isinstance(r.json(), list) else []
                for m in msgs:
                    mid = m.get("id", "")
                    if mid in seen_ids:
                        continue
                    if m.get("type") != "update":
                        continue
                    try:
                        payload = json.loads(m.get("content", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if payload.get("session_id") != session_id:
                        continue
                    seen_ids.add(mid)
                    updates.append({
                        "sender": m.get("sender"),
                        "content": payload.get("content", ""),
                        "timestamp": m.get("timestamp"),
                    })
                    if expected > 0 and len(updates) >= expected:
                        return updates
        except requests.RequestException:
            pass
        time.sleep(2)

    return updates


def _wait_for_participants(session_id: str, need: int, timeout: float = 30) -> List[str]:
    """Poll bus for join messages for this session. Returns participant names."""
    participants = []
    seen_ids = set()
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            r = requests.get(BUS_MESSAGES, params={"limit": 50, "topic": "convene"}, timeout=5)
            if r.ok:
                msgs = r.json() if isinstance(r.json(), list) else []
                for m in msgs:
                    mid = m.get("id", "")
                    if mid in seen_ids:
                        continue
                    if m.get("type") != "join":
                        continue
                    try:
                        payload = json.loads(m.get("content", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if payload.get("session_id") != session_id:
                        continue
                    seen_ids.add(mid)
                    worker = payload.get("worker", m.get("sender", "unknown"))
                    if worker not in participants:
                        participants.append(worker)
                        print(f"[convene] participant joined: {worker} ({len(participants)}/{need})")
                    if len(participants) >= need:
                        return participants
        except requests.RequestException:
            pass
        time.sleep(2)

    return participants


def convene_and_work(
    worker_name: str,
    topic: str,
    context: str,
    work_fn: Callable[[str, str, List[str]], str],
    need_workers: int = 2,
    wait_timeout: float = 30,
    collect_timeout: float = 30,
) -> Optional[str]:
    """High-level convene workflow:
    1. Initiate session
    2. Wait for participants (poll bus, timeout)
    3. Call work_fn(session_id, context, participants) -> result string
    4. Post result as update
    5. Collect updates from other participants
    6. Resolve session with combined summary
    Returns session_id or None on failure.
    """
    session_id = initiate_convene(worker_name, topic, context, need_workers)
    if not session_id:
        print("[convene] failed to initiate session", file=sys.stderr)
        return None

    print(f"[convene] session {session_id} created, waiting for {need_workers - 1} participant(s)...")
    participants = _wait_for_participants(session_id, need_workers - 1, wait_timeout)

    all_participants = [worker_name] + participants
    print(f"[convene] proceeding with participants: {all_participants}")

    # Execute work
    result = work_fn(session_id, context, all_participants)
    post_update(worker_name, session_id, result)

    # Collect updates from others
    updates = collect_updates(session_id, collect_timeout, expected=len(participants))

    # Build summary
    summary_parts = [f"{worker_name}: {result}"]
    for u in updates:
        summary_parts.append(f"{u['sender']}: {u['content']}")
    summary = " | ".join(summary_parts)

    resolve_session(worker_name, session_id, summary)
    print(f"[convene] session {session_id} resolved")
    return session_id


# ── CLI ───────────────────────────────────────────────────────────

# ── Persistent Session Store ──────────────────────────────────────

def _load_sessions() -> Dict[str, dict]:
    """Load persisted convene sessions from data/convene_sessions.json."""
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_sessions(sessions: Dict[str, dict]):
    """Persist convene sessions to disk."""
    DATA.mkdir(parents=True, exist_ok=True)
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2, default=str), encoding="utf-8")


class ConveneSession:
    """Manages a multi-worker collaboration session with local persistence."""

    def __init__(self, session_id: str = None):
        self._sessions = _load_sessions()
        self.session_id = session_id

    def initiate(self, topic: str, participants: List[str], context: str) -> str:
        """Create a session, notify participants via bus, persist locally."""
        sid = initiate_convene(participants[0] if participants else "orchestrator",
                               topic, context, len(participants))
        if not sid:
            sid = f"local_{int(time.time() * 1000)}"

        self.session_id = sid
        self._sessions[sid] = {
            "id": sid,
            "topic": topic,
            "context": context,
            "participants": participants,
            "contributions": {},
            "votes": {},
            "status": "active",
            "created_at": time.time(),
        }
        _save_sessions(self._sessions)

        # Notify each participant via bus
        for p in participants:
            try:
                requests.post(BUS_PUBLISH, json={
                    "sender": "convene",
                    "topic": "convene",
                    "type": "invite",
                    "content": json.dumps({"session_id": sid, "topic": topic, "worker": p}),
                }, timeout=3)
            except Exception:
                pass

        return sid

    def contribute(self, worker: str, content: str) -> bool:
        """Add a worker's contribution to the session."""
        self._sessions = _load_sessions()
        s = self._sessions.get(self.session_id)
        if not s:
            return False
        s["contributions"][worker] = {
            "content": content,
            "timestamp": time.time(),
        }
        _save_sessions(self._sessions)

        # Also post to bus so other systems can see
        post_update(worker, self.session_id, content)
        return True

    def get_contributions(self) -> Dict[str, dict]:
        """Return all contributions for this session."""
        self._sessions = _load_sessions()
        s = self._sessions.get(self.session_id, {})
        return s.get("contributions", {})

    def resolve(self, summary: str = None) -> str:
        """Synthesize all contributions into a resolution."""
        self._sessions = _load_sessions()
        s = self._sessions.get(self.session_id)
        if not s:
            return "Session not found"

        contributions = s.get("contributions", {})
        if not summary:
            parts = []
            for worker, c in contributions.items():
                parts.append(f"{worker}: {c.get('content', '')[:200]}")
            summary = " | ".join(parts) if parts else "No contributions"

        s["status"] = "resolved"
        s["resolution"] = summary
        s["resolved_at"] = time.time()
        _save_sessions(self._sessions)

        resolve_session("convene", self.session_id, summary)
        return summary


# ── Orchestrator-Managed Convene ──────────────────────────────────

def orchestrate_convene(topic: str, task_for_each_worker: str,
                        timeout: int = 60, workers: List[str] = None) -> dict:
    """Create a session, dispatch tasks via bus, collect and auto-resolve.

    Uses direct bus posts (no UIA dispatch) for lightweight coordination.
    """
    workers = workers or WORKER_NAMES
    session = ConveneSession()
    sid = session.initiate(topic, workers, task_for_each_worker)
    print(f"[convene] orchestrate session {sid}: {topic}")

    # Dispatch task to each worker via bus (direct bus posts, not API dispatch)
    for w in workers:
        try:
            requests.post(BUS_PUBLISH, json={
                "sender": "convene",
                "topic": "workers",
                "type": "convene-task",
                "content": json.dumps({
                    "session_id": sid,
                    "worker": w,
                    "task": task_for_each_worker,
                    "topic": topic,
                }),
            }, timeout=3)
            print(f"  -> dispatched to {w}")
        except Exception as e:
            print(f"  -> failed to dispatch to {w}: {e}")

    # Collect contributions
    deadline = time.time() + timeout
    seen_ids = set()
    contributed = set()

    while time.time() < deadline and len(contributed) < len(workers):
        try:
            r = requests.get(BUS_MESSAGES, params={"limit": 50, "topic": "convene"}, timeout=5)
            if r.ok:
                msgs = r.json() if isinstance(r.json(), list) else []
                for m in msgs:
                    mid = m.get("id", "")
                    if mid in seen_ids:
                        continue
                    if m.get("type") != "update":
                        continue
                    try:
                        payload = json.loads(m.get("content", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if payload.get("session_id") != sid:
                        continue
                    seen_ids.add(mid)
                    sender = m.get("sender", "")
                    if sender in workers and sender not in contributed:
                        session.contribute(sender, payload.get("content", ""))
                        contributed.add(sender)
                        print(f"  <- contribution from {sender} ({len(contributed)}/{len(workers)})")
        except Exception:
            pass
        if len(contributed) < len(workers):
            time.sleep(2)

    missing = [w for w in workers if w not in contributed]
    if missing:
        print(f"[convene] timeout: missing contributions from {missing}")

    resolution = session.resolve()
    print(f"[convene] resolved: {resolution[:120]}")

    return {
        "session_id": sid,
        "topic": topic,
        "contributions": session.get_contributions(),
        "resolution": resolution,
        "missing": missing,
    }


# ── Consensus Voting ─────────────────────────────────────────────

# ── ConveneGate — Governance Protocol ────────────────────────────

GATE_FILE = DATA / "convene_gate.json"

class ConveneGate:
    """Enforces convene-first communication protocol.

    Workers MUST convene before sending messages to the orchestrator.
    Only after majority agreement (2+ workers) does a message get elevated.
    Urgent reports (type='urgent') bypass the gate.
    """

    MAJORITY_THRESHOLD = 2  # minimum workers to approve

    def __init__(self):
        self._state = self._load()

    def _load(self) -> dict:
        if GATE_FILE.exists():
            try:
                return json.loads(GATE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"pending": {}, "elevated": [], "rejected": [], "stats": {
            "total_proposed": 0, "total_elevated": 0, "total_rejected": 0, "total_bypassed": 0
        }}

    def _save(self):
        DATA.mkdir(parents=True, exist_ok=True)
        GATE_FILE.write_text(json.dumps(self._state, indent=2, default=str), encoding="utf-8")

    def propose(self, worker: str, report: str, urgent: bool = False) -> dict:
        """Worker proposes a report to orchestrator. Returns gate decision."""
        self._state = self._load()
        self._state["stats"]["total_proposed"] = self._state["stats"].get("total_proposed", 0) + 1

        # Urgent bypass
        if urgent:
            self._state["stats"]["total_bypassed"] = self._state["stats"].get("total_bypassed", 0) + 1
            self._save()
            # Post directly to orchestrator
            try:
                requests.post(BUS_PUBLISH, json={
                    "sender": worker,
                    "topic": "orchestrator",
                    "type": "urgent",
                    "content": f"[URGENT BYPASS] {report}",
                }, timeout=3)
            except Exception:
                pass
            return {"action": "bypassed", "reason": "urgent", "delivered": True}

        # Check if similar proposal already pending
        gate_id = f"gate_{int(time.time() * 1000)}_{worker}"
        self._state["pending"][gate_id] = {
            "id": gate_id,
            "proposer": worker,
            "report": report,
            "votes": {worker: "YES"},  # proposer auto-votes YES
            "created_at": time.time(),
            "status": "pending",
        }
        self._save()

        # Post to convene topic for other workers to see
        try:
            requests.post(BUS_PUBLISH, json={
                "sender": worker,
                "topic": "convene",
                "type": "gate-proposal",
                "content": json.dumps({
                    "gate_id": gate_id,
                    "proposer": worker,
                    "report": report[:300],
                }),
            }, timeout=3)
        except Exception:
            pass

        return {"action": "proposed", "gate_id": gate_id, "votes": 1,
                "needed": self.MAJORITY_THRESHOLD}

    def vote_gate(self, gate_id: str, worker: str, approve: bool) -> dict:
        """Worker votes on a pending gate proposal."""
        self._state = self._load()
        p = self._state["pending"].get(gate_id)
        if not p:
            return {"error": "gate proposal not found"}
        if p["status"] != "pending":
            return {"error": f"proposal already {p['status']}"}

        p["votes"][worker] = "YES" if approve else "NO"
        yes_count = sum(1 for v in p["votes"].values() if v == "YES")
        no_count = sum(1 for v in p["votes"].values() if v == "NO")

        # Check if majority reached
        if yes_count >= self.MAJORITY_THRESHOLD:
            return self._elevate(gate_id, p)
        elif no_count >= self.MAJORITY_THRESHOLD:
            return self._reject(gate_id, p)

        self._save()

        # Broadcast vote
        try:
            requests.post(BUS_PUBLISH, json={
                "sender": worker,
                "topic": "convene",
                "type": "gate-vote",
                "content": json.dumps({
                    "gate_id": gate_id,
                    "worker": worker,
                    "vote": "YES" if approve else "NO",
                    "yes_count": yes_count,
                    "needed": self.MAJORITY_THRESHOLD,
                }),
            }, timeout=3)
        except Exception:
            pass

        return {"action": "voted", "gate_id": gate_id, "yes": yes_count,
                "no": no_count, "needed": self.MAJORITY_THRESHOLD}

    def _elevate(self, gate_id: str, proposal: dict) -> dict:
        """Majority reached -- elevate report to orchestrator."""
        proposal["status"] = "elevated"
        proposal["elevated_at"] = time.time()
        self._state["elevated"].append({
            "gate_id": gate_id,
            "report": proposal["report"],
            "proposer": proposal["proposer"],
            "voters": proposal["votes"],
            "elevated_at": proposal["elevated_at"],
        })
        del self._state["pending"][gate_id]
        self._state["stats"]["total_elevated"] = self._state["stats"].get("total_elevated", 0) + 1
        self._save()

        # Post consensus result to orchestrator
        voters = [w for w, v in proposal["votes"].items() if v == "YES"]
        try:
            requests.post(BUS_PUBLISH, json={
                "sender": "convene-gate",
                "topic": "orchestrator",
                "type": "result",
                "content": f"[CONSENSUS {len(voters)}/{len(proposal['votes'])}] "
                           f"Proposed by {proposal['proposer']}, endorsed by {', '.join(voters)}: "
                           f"{proposal['report'][:300]}",
            }, timeout=3)
        except Exception:
            pass

        return {"action": "elevated", "gate_id": gate_id,
                "voters": voters, "delivered": True}

    def _reject(self, gate_id: str, proposal: dict) -> dict:
        """Majority rejected -- do not elevate."""
        proposal["status"] = "rejected"
        proposal["rejected_at"] = time.time()
        self._state["rejected"].append({
            "gate_id": gate_id,
            "report": proposal["report"][:200],
            "proposer": proposal["proposer"],
            "voters": proposal["votes"],
            "rejected_at": proposal["rejected_at"],
        })
        del self._state["pending"][gate_id]
        self._state["stats"]["total_rejected"] = self._state["stats"].get("total_rejected", 0) + 1
        self._save()

        return {"action": "rejected", "gate_id": gate_id}

    def get_pending(self) -> dict:
        """Return all pending proposals."""
        self._state = self._load()
        return self._state.get("pending", {})

    def get_stats(self) -> dict:
        """Return gate statistics."""
        self._state = self._load()
        return self._state.get("stats", {})

    def expire_stale(self, max_age_s: int = 300):
        """Expire proposals older than max_age_s that haven't reached consensus."""
        self._state = self._load()
        now = time.time()
        expired = []
        for gid, p in list(self._state["pending"].items()):
            if now - p.get("created_at", 0) > max_age_s:
                p["status"] = "expired"
                self._state["rejected"].append({
                    "gate_id": gid,
                    "report": p["report"][:200],
                    "proposer": p["proposer"],
                    "reason": "expired",
                })
                expired.append(gid)
                del self._state["pending"][gid]
        if expired:
            self._save()
        return expired


# ── Original Consensus Voting ────────────────────────────────────

def vote(session_id: str, worker: str, proposal: str, choice: str) -> bool:
    """Cast a vote on a proposal. choice: YES, NO, or ABSTAIN."""
    choice = choice.upper()
    if choice not in ("YES", "NO", "ABSTAIN"):
        print(f"[convene] invalid vote '{choice}', must be YES/NO/ABSTAIN", file=sys.stderr)
        return False

    sessions = _load_sessions()
    s = sessions.get(session_id)
    if not s:
        print(f"[convene] session {session_id} not found", file=sys.stderr)
        return False

    if "votes" not in s:
        s["votes"] = {}
    if proposal not in s["votes"]:
        s["votes"][proposal] = {}
    s["votes"][proposal][worker] = {"choice": choice, "timestamp": time.time()}
    _save_sessions(sessions)

    # Also broadcast vote to bus
    try:
        requests.post(BUS_PUBLISH, json={
            "sender": worker,
            "topic": "convene",
            "type": "vote",
            "content": json.dumps({
                "session_id": session_id,
                "proposal": proposal,
                "choice": choice,
            }),
        }, timeout=3)
    except Exception:
        pass

    return True


def consensus(session_id: str, proposal: str, quorum_pct: float = 50.0) -> dict:
    """Check consensus on a proposal. Returns result when quorum (>50%) reached."""
    sessions = _load_sessions()
    s = sessions.get(session_id, {})
    votes = s.get("votes", {}).get(proposal, {})

    total_voters = len(s.get("participants", WORKER_NAMES))
    cast = len(votes)
    yes_count = sum(1 for v in votes.values() if v.get("choice") == "YES")
    no_count = sum(1 for v in votes.values() if v.get("choice") == "NO")
    abstain_count = sum(1 for v in votes.values() if v.get("choice") == "ABSTAIN")

    quorum_needed = int(total_voters * quorum_pct / 100) + 1
    quorum_met = cast >= quorum_needed

    if quorum_met:
        if yes_count > no_count:
            result = "APPROVED"
        elif no_count > yes_count:
            result = "REJECTED"
        else:
            result = "TIED"
    else:
        result = "PENDING"

    return {
        "proposal": proposal,
        "result": result,
        "quorum_met": quorum_met,
        "votes_cast": cast,
        "quorum_needed": quorum_needed,
        "total_voters": total_voters,
        "yes": yes_count,
        "no": no_count,
        "abstain": abstain_count,
        "voters": {w: v.get("choice") for w, v in votes.items()},
    }


# ── CLI ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Skynet Convene Session Helper")
    parser.add_argument("--initiate", action="store_true", help="Create a new convene session")
    parser.add_argument("--discover", action="store_true", help="List active sessions")
    parser.add_argument("--join", type=str, metavar="SESSION_ID", help="Join an existing session")
    parser.add_argument("--status", action="store_true", help="Show all sessions with details")
    parser.add_argument("--orchestrate", type=str, metavar="TOPIC", help="Orchestrate a convene session")
    parser.add_argument("--vote-session", type=str, metavar="SESSION_ID", help="Vote on a proposal")
    parser.add_argument("--consensus", type=str, metavar="SESSION_ID", help="Check consensus")
    parser.add_argument("--worker", type=str, default="beta", help="Worker name (default: beta)")
    parser.add_argument("--topic", type=str, default="", help="Session topic (for --initiate)")
    parser.add_argument("--context", type=str, default="", help="Session context (for --initiate)")
    parser.add_argument("--need", type=int, default=2, help="Workers needed (for --initiate)")
    parser.add_argument("--timeout", type=int, default=60, help="Timeout for --orchestrate")
    parser.add_argument("--task", type=str, default="", help="Task for each worker (--orchestrate)")
    parser.add_argument("--proposal", type=str, default="", help="Proposal text (for --vote/--consensus)")
    parser.add_argument("--choice", type=str, default="YES", help="Vote choice: YES/NO/ABSTAIN")
    args = parser.parse_args()

    if args.initiate:
        if not args.topic:
            print("--topic required for --initiate", file=sys.stderr)
            sys.exit(1)
        sid = initiate_convene(args.worker, args.topic, args.context, args.need)
        if sid:
            print(f"Session created: {sid}")
        else:
            print("Failed to create session", file=sys.stderr)
            sys.exit(1)

    elif args.discover:
        sessions = discover_sessions()
        if not sessions:
            print("No active sessions")
            return
        for s in sessions:
            status = s.get("status", "?")
            participants = ", ".join(s.get("participants", []))
            need = s.get("need_workers", "?")
            print(f"  {s.get('id')}  [{status}]  topic={s.get('topic')}  "
                  f"participants=[{participants}]  need={need}  "
                  f"initiator={s.get('initiator')}")

    elif args.join:
        ok = join_session(args.worker, args.join)
        if ok:
            print(f"{args.worker} joined session {args.join}")
        else:
            print(f"Failed to join session {args.join}", file=sys.stderr)
            sys.exit(1)

    elif args.status:
        sessions = discover_sessions()
        local = _load_sessions()
        # Merge local sessions for display
        all_sessions = {s.get("id"): s for s in sessions}
        for sid, s in local.items():
            if sid not in all_sessions:
                all_sessions[sid] = s
        if not all_sessions:
            print("No sessions")
            return
        for sid, s in all_sessions.items():
            print(f"\n{'='*60}")
            print(f"  Session:      {s.get('id', sid)}")
            print(f"  Status:       {s.get('status')}")
            print(f"  Topic:        {s.get('topic')}")
            print(f"  Context:      {s.get('context', '')[:80]}")
            print(f"  Initiator:    {s.get('initiator', '-')}")
            participants = s.get("participants", [])
            print(f"  Participants: {', '.join(participants) if isinstance(participants, list) else participants}")
            print(f"  Need:         {s.get('need_workers', '-')}")
            contribs = s.get("contributions", {})
            if contribs:
                print(f"  Contributions: {len(contribs)}")
                for w, c in contribs.items():
                    ct = c.get("content", str(c))[:80] if isinstance(c, dict) else str(c)[:80]
                    print(f"    [{w}] {ct}")
            votes_data = s.get("votes", {})
            if votes_data:
                print(f"  Votes:")
                for prop, vv in votes_data.items():
                    print(f"    Proposal: {prop}")
                    for w, v in vv.items():
                        ch = v.get("choice", v) if isinstance(v, dict) else v
                        print(f"      {w}: {ch}")
            if s.get("resolution"):
                print(f"  Resolution:   {s['resolution'][:120]}")
            msgs = s.get("messages", [])
            if msgs:
                print(f"  Messages:     {len(msgs)}")
                for m in msgs[-5:]:
                    print(f"    [{m.get('sender')}] {m.get('content', '')[:80]}")
        print()

    elif args.orchestrate:
        task = args.task or f"Contribute to: {args.orchestrate}"
        result = orchestrate_convene(args.orchestrate, task, args.timeout)
        print(json.dumps(result, indent=2, default=str))

    elif args.vote_session:
        if not args.proposal:
            print("--proposal required for --vote-session", file=sys.stderr)
            sys.exit(1)
        ok = vote(args.vote_session, args.worker, args.proposal, args.choice)
        if ok:
            print(f"{args.worker} voted {args.choice} on '{args.proposal}'")
        else:
            sys.exit(1)

    elif args.consensus:
        if not args.proposal:
            print("--proposal required for --consensus", file=sys.stderr)
            sys.exit(1)
        result = consensus(args.consensus, args.proposal)
        print(json.dumps(result, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
