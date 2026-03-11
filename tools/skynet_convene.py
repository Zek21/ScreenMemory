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
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests

from tools.skynet_spam_guard import guarded_publish  # signed: alpha

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SESSIONS_FILE = DATA / "convene_sessions.json"
WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

SKYNET = "http://localhost:8420"
BUS_PUBLISH = f"{SKYNET}/bus/publish"
BUS_MESSAGES = f"{SKYNET}/bus/messages"
BUS_CONVENE = f"{SKYNET}/bus/convene"
TODOS_FILE = DATA / "todos.json"


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
        # Also post bus message for visibility  # signed: alpha
        result = guarded_publish({
            "sender": worker_name,
            "topic": "convene",
            "type": "join",
            "content": json.dumps({"session_id": session_id, "worker": worker_name}),
        })
        return result.get("allowed", False)
    except requests.RequestException as e:
        print(f"[convene] join error: {e}", file=sys.stderr)
    return False


def post_update(worker_name: str, session_id: str, content: str) -> bool:
    """Post a work update to the convene session."""
    try:
        result = guarded_publish({  # signed: alpha
            "sender": worker_name,
            "topic": "convene",
            "type": "update",
            "content": json.dumps({"session_id": session_id, "content": content}),
        })
        return result.get("allowed", False)
    except requests.RequestException as e:
        print(f"[convene] update error: {e}", file=sys.stderr)
    return False


def resolve_session(worker_name: str, session_id: str, summary: str) -> bool:
    """Close a convene session via DELETE /bus/convene and post summary to bus."""
    try:
        # Mark session as resolved in Go backend
        requests.delete(f"{BUS_CONVENE}?id={session_id}", timeout=5)
        # Post summary to bus for visibility
        result = guarded_publish({  # signed: alpha
            "sender": worker_name,
            "topic": "convene",
            "type": "resolve",
            "content": json.dumps({"session_id": session_id, "summary": summary}),
        })
        return result.get("allowed", False)
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
                guarded_publish({  # signed: alpha
                    "sender": "convene",
                    "topic": "convene",
                    "type": "invite",
                    "content": json.dumps({"session_id": sid, "topic": topic, "worker": p}),
                })
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

def _dispatch_convene_tasks(workers: List[str], sid: str,
                            task_for_each_worker: str) -> None:
    """Post convene-task messages to each worker via bus."""
    for w in workers:
        try:
            guarded_publish({  # signed: alpha
                "sender": "convene",
                "topic": "workers",
                "type": "convene-task",
                "content": json.dumps({
                    "session_id": sid,
                    "worker": w,
                    "task": task_for_each_worker,
                    "topic": "convene",
                }),
            })
            print(f"  -> dispatched to {w}")
        except Exception as e:
            print(f"  -> failed to dispatch to {w}: {e}")


def _collect_contributions(session: 'ConveneSession', sid: str,
                           workers: List[str], timeout: int) -> List[str]:
    """Poll bus for convene updates and return list of missing workers."""
    deadline = time.time() + timeout
    seen_ids: set = set()
    contributed: set = set()

    while time.time() < deadline and len(contributed) < len(workers):
        try:
            r = requests.get(BUS_MESSAGES, params={"limit": 50, "topic": "convene"}, timeout=5)
            if r.ok:
                msgs = r.json() if isinstance(r.json(), list) else []
                for m in msgs:
                    mid = m.get("id", "")
                    if mid in seen_ids or m.get("type") != "update":
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

    return [w for w in workers if w not in contributed]


def orchestrate_convene(topic: str, task_for_each_worker: str,
                        timeout: int = 60, workers: List[str] = None) -> dict:
    """Create a session, dispatch tasks via bus, collect and auto-resolve."""
    workers = workers or WORKER_NAMES
    session = ConveneSession()
    sid = session.initiate(topic, workers, task_for_each_worker)
    print(f"[convene] orchestrate session {sid}: {topic}")

    _dispatch_convene_tasks(workers, sid, task_for_each_worker)
    missing = _collect_contributions(session, sid, workers, timeout)
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
    RESEND_INTERVAL_S = 900
    DELIVERY_INTERVAL_S = 1800
    DELIVERY_TYPE = "elevated_digest"
    GENERIC_REPORTS = {
        "important finding",
        "fix needed",
        "proposal",
        "report",
        "issue found",
        "bug found",
        "needs fix",
    }
    STOPWORDS = {
        "the", "and", "for", "with", "this", "that", "from", "into", "need",
        "needs", "needed", "found", "finding", "fix", "important", "report",
        "issue", "a", "an", "to", "of", "on", "in", "is", "are", "be",
    }
    ARCHITECTURE_SIGNALS = {
        "architecture", "auth", "backend", "bridge", "cache", "constructor",
        "daemon", "dashboard", "dispatch", "embedder", "endpoint", "engine",
        "frontend", "heartbeat", "monitor", "ocr", "orchestrator", "pipeline",
        "probe", "queue", "realtime", "refresh", "routing", "session",
        "status", "timeout", "token", "ttl", "uia", "worker",
    }
    ARCHITECTURE_HINTS = {
        "because", "cache", "calls", "constructor", "current", "currently",
        "daemon", "endpoint", "flow", "instantiat", "path", "poll", "probe",
        "queue", "route", "status", "ttl", "uses", "via",
    }
    REALISTIC_FIX_HINTS = {
        "add", "change", "decrease", "display", "fallback", "fix", "increase",
        "instead", "limit", "mark", "propose", "queue", "route", "show",
        "skip", "solution", "timeout", "use",
    }
    ISSUE_CONCERN_HINTS = {
        "badge": "badge",
        "bridge": "bridge",
        "cache": "cache",
        "constructor": "constructor",
        "daemon": "daemon",
        "fixation": "fixation",
        "health": "health",
        "import_only": "import_only",
        "instantiat": "instantiate",
        "latency": "latency",
        "probe": "probe",
        "queue": "queue",
        "refresh": "refresh",
        "retry": "retry",
        "rotat": "rotate",
        "routing": "routing",
        "session": "session",
        "stale": "stale",
        "timeout": "timeout",
        "timestamp": "timestamp",
        "token": "token",
        "ttl": "ttl",
    }
    CODE_REFERENCE_RE = re.compile(
        r"(?:\b[a-zA-Z0-9_./-]+\.py(?::\d+)?\b)|"
        r"(?:\b[a-zA-Z_][a-zA-Z0-9_]*\(\))|"
        r"(?:/[A-Za-z0-9_./-]+)|"
        r"(?:\b[A-Z_]{3,}\b)"
    )

    def __init__(self):
        self._state = self._load()

    @staticmethod
    def _canonical_report(report: str) -> str:
        text = re.sub(r"\s+", " ", str(report or "").strip().lower())
        return text

    @classmethod
    def _report_fingerprint(cls, report: str) -> str:
        canonical = cls._canonical_report(report)
        return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _report_keywords(cls, report: str) -> List[str]:
        words = re.findall(r"[a-z0-9_./-]{3,}", cls._canonical_report(report))
        keywords = [w for w in words if w not in cls.STOPWORDS]
        seen = set()
        ordered = []
        for word in keywords:
            if word in seen:
                continue
            seen.add(word)
            ordered.append(word)
        return ordered[:8]

    @classmethod
    def _architecture_signal_hits(cls, report: str) -> List[str]:
        return [kw for kw in cls._report_keywords(report) if kw in cls.ARCHITECTURE_SIGNALS]

    @classmethod
    def _report_code_refs(cls, report: str) -> List[str]:
        refs = []
        seen = set()
        for ref in cls.CODE_REFERENCE_RE.findall(str(report or "")):
            normalized = str(ref or "").strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            refs.append(normalized)
        return refs[:10]

    @classmethod
    def _report_concern_terms(cls, report: str) -> List[str]:
        canonical = cls._canonical_report(report)
        hits = []
        for hint, label in cls.ISSUE_CONCERN_HINTS.items():
            if hint in canonical and label not in hits:
                hits.append(label)
        return hits[:6]

    @classmethod
    def _report_issue_key(cls, report: str) -> str | None:
        refs = cls._report_code_refs(report)
        concerns = cls._report_concern_terms(report)
        parts = []
        if cls._is_architecture_sensitive(report):
            parts.extend([r for r in refs if ".py" in r or r.endswith("()") or r.startswith("/")][:4])
            parts.extend(concerns[:3])
        elif refs and concerns:
            parts.extend(refs[:3])
            parts.extend(concerns[:2])
        if len(parts) < 2:
            return None
        signature = "|".join(sorted(dict.fromkeys(parts)))
        return hashlib.sha1(signature.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _is_architecture_sensitive(cls, report: str) -> bool:
        canonical = cls._canonical_report(report)
        hits = cls._architecture_signal_hits(report)
        if len(hits) >= 2:
            return True
        return bool(hits) and any(
            phrase in canonical
            for phrase in ("bottleneck", "cache age", "session fixation", "throughput", "stale")
        )

    @classmethod
    def _has_architecture_backing(cls, report: str) -> bool:
        canonical = cls._canonical_report(report)
        if len(canonical) < 80:
            return False
        if not cls.CODE_REFERENCE_RE.search(str(report or "")):
            return False
        mechanism_hits = sum(1 for hint in cls.ARCHITECTURE_HINTS if hint in canonical)
        fix_hits = sum(1 for hint in cls.REALISTIC_FIX_HINTS if hint in canonical)
        return mechanism_hits >= 2 and fix_hits >= 1

    @classmethod
    def _classify_report(cls, report: str) -> tuple[str, str]:
        canonical = cls._canonical_report(report)
        words = canonical.split()
        if not canonical:
            return "invalid", "empty_report"
        if canonical in cls.GENERIC_REPORTS:
            return "low_signal", "generic_report"
        if len(canonical) < 24:
            return "low_signal", "too_short"
        if len(words) < 4:
            return "low_signal", "insufficient_detail"
        if len(cls._report_keywords(report)) < 2:
            return "low_signal", "insufficient_specific_keywords"
        return "valid", ""

    @staticmethod
    def _ts_from_iso(value: str) -> float:
        try:
            return datetime.fromisoformat(str(value)).timestamp()
        except Exception:
            return 0.0

    @classmethod
    def _text_matches_keywords(cls, text: str, keywords: List[str]) -> bool:
        haystack = cls._canonical_report(text)
        if not haystack or not keywords:
            return False
        hits = sum(1 for kw in keywords if kw in haystack)
        needed = 2 if len(keywords) >= 3 else 1
        return hits >= needed

    def _normalize_state(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("pending", {})
        raw.setdefault("elevated", [])
        raw.setdefault("queued", [])
        raw.setdefault("rejected", [])
        raw.setdefault("delivery_queue", [])
        raw.setdefault("active_findings", {})
        stats = raw.setdefault("stats", {})
        stats.setdefault("total_proposed", 0)
        stats.setdefault("total_elevated", 0)
        stats.setdefault("total_queued", 0)
        stats.setdefault("total_rejected", 0)
        stats.setdefault("total_bypassed", 0)
        stats.setdefault("total_suppressed", 0)
        stats.setdefault("total_invalid", 0)
        stats.setdefault("total_architecture_review_queued", 0)
        stats.setdefault("total_digest_deliveries", 0)
        return raw

    def _load_recent_bus_messages(self, limit: int = 120) -> List[dict]:
        try:
            r = requests.get(BUS_MESSAGES, params={"limit": limit}, timeout=3)
            if r.ok:
                payload = r.json()
                return payload if isinstance(payload, list) else []
        except requests.RequestException:
            pass
        return []

    def _detect_action(self, finding: dict) -> tuple[bool, str]:
        gate_id = str(finding.get("last_gate_id") or "")
        keywords = list(finding.get("keywords") or [])
        elevated_at = float(finding.get("last_elevated_at") or 0.0)
        for msg in self._load_recent_bus_messages():
            sender = str(msg.get("sender") or "")
            if sender == "convene-gate":
                continue
            ts = self._ts_from_iso(msg.get("timestamp"))
            if ts and ts <= elevated_at:
                continue
            content = str(msg.get("content") or "")
            topic = str(msg.get("topic") or "")
            msg_type = str(msg.get("type") or "")
            if gate_id and gate_id in content:
                return True, f"bus:{sender}:{msg_type}"
            if sender in {"orchestrator", "consultant", "gemini_consultant"}:
                if self._text_matches_keywords(content, keywords):
                    return True, f"bus:{sender}:{msg_type}"
            if topic in {"workers", "todos"} and self._text_matches_keywords(content, keywords):
                return True, f"bus:{sender}:{msg_type}"

        if TODOS_FILE.exists():
            try:
                todos = json.loads(TODOS_FILE.read_text(encoding="utf-8")).get("todos", [])
            except (json.JSONDecodeError, OSError):
                todos = []
            for item in todos:
                text = str(item.get("title") or item.get("task") or "")
                if self._text_matches_keywords(text, keywords):
                    return True, f"todo:{item.get('id', '')}"
        return False, ""

    def _check_duplicate_finding(self, report: str) -> dict | None:
        now = time.time()
        fingerprint = self._report_fingerprint(report)
        issue_key = self._report_issue_key(report)
        active_findings = self._state.get("active_findings", {})
        finding = active_findings.get(fingerprint)
        if not finding and issue_key:
            for candidate in active_findings.values():
                if candidate.get("issue_key") == issue_key:
                    finding = candidate
                    break
        if not finding:
            return None
        resolved_fingerprint = finding.get("fingerprint", fingerprint)
        if finding.get("action_taken"):
            return {
                "action": "suppressed",
                "reason": "action_detected",
                "fingerprint": resolved_fingerprint,
                "last_gate_id": finding.get("last_gate_id"),
                "action_reason": finding.get("action_reason", ""),
                "issue_key": finding.get("issue_key"),
            }
        action_taken, action_reason = self._detect_action(finding)
        if action_taken:
            finding["action_taken"] = True
            finding["action_detected_at"] = now
            finding["action_reason"] = action_reason
            self._save()
            return {
                "action": "suppressed",
                "reason": "action_detected",
                "fingerprint": resolved_fingerprint,
                "last_gate_id": finding.get("last_gate_id"),
                "action_reason": action_reason,
                "issue_key": finding.get("issue_key"),
            }
        last_elevated = float(finding.get("last_elevated_at") or 0.0)
        if now - last_elevated < self.RESEND_INTERVAL_S:
            remaining = int(self.RESEND_INTERVAL_S - (now - last_elevated))
            return {
                "action": "suppressed",
                "reason": "awaiting_action_cooldown",
                "fingerprint": resolved_fingerprint,
                "last_gate_id": finding.get("last_gate_id"),
                "retry_after_s": remaining,
                "issue_key": finding.get("issue_key"),
            }
        return None

    def _record_active_finding(self, gate_id: str, proposal: dict, voters: List[str]) -> None:
        fingerprint = self._report_fingerprint(proposal["report"])
        issue_key = proposal.get("issue_key") or self._report_issue_key(proposal["report"])
        self._state["active_findings"][fingerprint] = {
            "fingerprint": fingerprint,
            "issue_key": issue_key,
            "report": proposal["report"],
            "keywords": self._report_keywords(proposal["report"]),
            "proposer": proposal["proposer"],
            "last_gate_id": gate_id,
            "last_elevated_at": proposal["elevated_at"],
            "last_voters": voters,
            "action_taken": False,
            "action_detected_at": None,
            "action_reason": "",
        }

    def _resolve_active_finding(self, fingerprint: str, issue_key: str) -> dict | None:
        finding = self._state.get("active_findings", {}).get(fingerprint)
        if finding or not issue_key:
            return finding
        for candidate in self._state.get("active_findings", {}).values():
            if candidate.get("issue_key") == issue_key:
                return candidate
        return None

    def _queue_digest_delivery(self, gate_id: str, proposal: dict, voters: List[str]) -> None:
        fingerprint = proposal.get("fingerprint") or self._report_fingerprint(proposal["report"])
        issue_key = proposal.get("issue_key") or self._report_issue_key(proposal["report"])
        for entry in self._state.get("delivery_queue", []):
            if entry.get("status") != "pending":
                continue
            same_issue = entry.get("fingerprint") == fingerprint
            if not same_issue and issue_key:
                same_issue = entry.get("issue_key") == issue_key
            if not same_issue:
                continue
            entry["gate_id"] = gate_id
            entry["report"] = proposal["report"]
            entry["proposer"] = proposal["proposer"]
            entry["voters"] = voters
            entry["vote_count"] = len(voters)
            entry["vote_total"] = len(proposal.get("votes", {}))
            entry["last_elevated_at"] = proposal["elevated_at"]
            entry["repeat_count"] = int(entry.get("repeat_count") or 1) + 1
            return

        self._state["delivery_queue"].append({
            "gate_id": gate_id,
            "report": proposal["report"],
            "proposer": proposal["proposer"],
            "voters": voters,
            "vote_count": len(voters),
            "vote_total": len(proposal.get("votes", {})),
            "fingerprint": fingerprint,
            "issue_key": issue_key,
            "queued_at": proposal["elevated_at"],
            "last_elevated_at": proposal["elevated_at"],
            "status": "pending",
            "repeat_count": 1,
            "delivery_type": self.DELIVERY_TYPE,
        })

    def _flush_due_digest(self, force: bool = False, now: float | None = None) -> dict:
        now = float(now or time.time())
        pending = [
            entry for entry in self._state.get("delivery_queue", [])
            if entry.get("status") == "pending"
        ]
        if not pending:
            return {
                "action": "noop",
                "reason": "empty",
                "delivery_type": self.DELIVERY_TYPE,
            }

        oldest = min(float(entry.get("queued_at") or entry.get("last_elevated_at") or now) for entry in pending)
        elapsed = now - oldest
        if not force and elapsed < self.DELIVERY_INTERVAL_S:
            return {
                "action": "noop",
                "reason": "cooldown",
                "delivery_type": self.DELIVERY_TYPE,
                "retry_after_s": max(1, int(self.DELIVERY_INTERVAL_S - elapsed)),
            }

        ready = []
        suppressed = 0
        for entry in pending:
            finding = self._resolve_active_finding(
                str(entry.get("fingerprint") or ""),
                str(entry.get("issue_key") or ""),
            )
            if finding:
                action_taken = bool(finding.get("action_taken"))
                action_reason = str(finding.get("action_reason") or "")
                if not action_taken:
                    action_taken, action_reason = self._detect_action(finding)
                    if action_taken:
                        finding["action_taken"] = True
                        finding["action_detected_at"] = now
                        finding["action_reason"] = action_reason
                if action_taken:
                    entry["status"] = "suppressed"
                    entry["suppressed_at"] = now
                    entry["action_reason"] = action_reason
                    suppressed += 1
                    continue
            ready.append(entry)

        if not ready:
            self._save()
            return {
                "action": "noop",
                "reason": "action_taken",
                "delivery_type": self.DELIVERY_TYPE,
                "suppressed": suppressed,
            }

        try:
            from skynet_delivery import deliver_elevated_digest
            result = deliver_elevated_digest(ready, window_seconds=self.DELIVERY_INTERVAL_S)
        except Exception as exc:
            result = {
                "success": False,
                "detail": str(exc),
                "delivery_type": self.DELIVERY_TYPE,
            }

        if result.get("success"):
            for entry in ready:
                entry["status"] = "delivered"
                entry["delivered_at"] = now
            self._state["stats"]["total_digest_deliveries"] = (
                self._state["stats"].get("total_digest_deliveries", 0) + 1
            )
            self._state["stats"]["last_digest_delivery_at"] = now
            self._save()
            return {
                "action": "delivered",
                "delivery_type": self.DELIVERY_TYPE,
                "count": len(ready),
                "suppressed": suppressed,
                "success": True,
            }

        for entry in ready:
            entry["last_delivery_attempt_at"] = now
        self._save()
        return {
            "action": "pending",
            "reason": "delivery_failed",
            "delivery_type": self.DELIVERY_TYPE,
            "count": len(ready),
            "suppressed": suppressed,
            "success": False,
        }

    def flush_due_digest(self, force: bool = False, now: float | None = None) -> dict:
        self._state = self._load()
        return self._flush_due_digest(force=force, now=now)

    def _queue_report_for_review(
        self,
        worker: str,
        report: str,
        reason: str,
        review_kind: str = "cross_validation",
    ) -> dict:
        fingerprint = self._report_fingerprint(report)
        issue_key = self._report_issue_key(report)
        try:
            from tools import skynet_todos as todos
        except ModuleNotFoundError:
            import skynet_todos as todos

        if review_kind == "architecture_review":
            queue_text = (
                f"Architecture review required for finding from {worker}: {report}\n"
                f"Deliverables: map current files/functions/endpoints or daemons involved; "
                f"state why the current architecture behaves this way; determine whether the claim is true; "
                f"propose a realistic fix with risks/tradeoffs."
            )
            action = "queued_for_architecture_review"
            priority = "high"
        else:
            queue_text = (
                f"Cross-validate and enrich low-signal convene finding from {worker}: {report}"
            )
            action = "queued_for_cross_validation"
            priority = "normal"
        item = todos.add_todo("shared", queue_text, priority=priority)
        now = time.time()
        self._state["queued"].append({
            "queue_id": item["id"],
            "report": report,
            "proposer": worker,
            "reason": reason,
            "review_kind": review_kind,
            "queued_at": now,
            "fingerprint": fingerprint,
            "issue_key": issue_key,
        })
        self._state["stats"]["total_queued"] = self._state["stats"].get("total_queued", 0) + 1
        if review_kind == "architecture_review":
            self._state["stats"]["total_architecture_review_queued"] = (
                self._state["stats"].get("total_architecture_review_queued", 0) + 1
            )
        self._state["active_findings"][fingerprint] = {
            "fingerprint": fingerprint,
            "issue_key": issue_key,
            "report": report,
            "keywords": self._report_keywords(report),
            "proposer": worker,
            "last_gate_id": item["id"],
            "last_elevated_at": now,
            "last_voters": [worker],
            "action_taken": True,
            "action_detected_at": now,
            "action_reason": f"todo:{item['id']}",
            "delivery_mode": "queue_only",
            "review_kind": review_kind,
        }
        self._save()
        try:
            guarded_publish({  # signed: alpha
                "sender": "convene-gate",
                "topic": "convene",
                "type": "gate-queued",
                "content": json.dumps({
                    "queue_id": item["id"],
                    "proposer": worker,
                    "reason": reason,
                    "review_kind": review_kind,
                    "report": report[:300],
                }),
            })
        except Exception:
            pass
        return {
            "action": action,
            "reason": reason,
            "queue_id": item["id"],
            "fingerprint": fingerprint,
            "review_kind": review_kind,
            "issue_key": issue_key,
        }

    def _load(self) -> dict:
        if GATE_FILE.exists():
            try:
                return self._normalize_state(json.loads(GATE_FILE.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass
        return self._normalize_state({})

    def _save(self):
        DATA.mkdir(parents=True, exist_ok=True)
        GATE_FILE.write_text(json.dumps(self._state, indent=2, default=str), encoding="utf-8")

    def propose(self, worker: str, report: str, urgent: bool = False) -> dict:
        """Worker proposes a report to orchestrator. Returns gate decision."""
        self._state = self._load()
        self._flush_due_digest()

        # Urgent bypass
        if urgent:
            self._state["stats"]["total_proposed"] = self._state["stats"].get("total_proposed", 0) + 1
            self._state["stats"]["total_bypassed"] = self._state["stats"].get("total_bypassed", 0) + 1
            self._save()
            # Post directly to orchestrator
            try:
                guarded_publish({  # signed: alpha
                    "sender": worker,
                    "topic": "orchestrator",
                    "type": "urgent",
                    "content": f"[URGENT BYPASS] {report}",
                })
            except Exception:
                pass
            return {"action": "bypassed", "reason": "urgent", "delivered": True}

        classification, reason = self._classify_report(report)
        if classification == "invalid":
            self._state["stats"]["total_invalid"] = self._state["stats"].get("total_invalid", 0) + 1
            self._save()
            return {"action": "rejected", "reason": reason, "delivered": False}

        duplicate = self._check_duplicate_finding(report)
        if duplicate:
            self._state["stats"]["total_suppressed"] = self._state["stats"].get("total_suppressed", 0) + 1
            self._save()
            return duplicate

        if classification == "low_signal":
            return self._queue_report_for_review(worker, report, reason)

        if self._is_architecture_sensitive(report) and not self._has_architecture_backing(report):
            return self._queue_report_for_review(
                worker,
                report,
                "architecture_review_required",
                review_kind="architecture_review",
            )

        self._state["stats"]["total_proposed"] = self._state["stats"].get("total_proposed", 0) + 1

        gate_id = f"gate_{int(time.time() * 1000)}_{worker}"
        self._state["pending"][gate_id] = {
            "id": gate_id,
            "proposer": worker,
            "report": report,
            "fingerprint": self._report_fingerprint(report),
            "issue_key": self._report_issue_key(report),
            "keywords": self._report_keywords(report),
            "votes": {worker: "YES"},  # proposer auto-votes YES
            "created_at": time.time(),
            "status": "pending",
        }
        self._save()

        # Post to convene topic for other workers to see
        try:
            guarded_publish({  # signed: alpha
                "sender": worker,
                "topic": "convene",
                "type": "gate-proposal",
                "content": json.dumps({
                    "gate_id": gate_id,
                    "proposer": worker,
                    "report": report[:300],
                }),
            })
        except Exception:
            pass

        return {"action": "proposed", "gate_id": gate_id, "votes": 1,
                "needed": self.MAJORITY_THRESHOLD}

    def vote_gate(self, gate_id: str, worker: str, approve: bool) -> dict:
        """Worker votes on a pending gate proposal."""
        self._state = self._load()
        self._flush_due_digest()
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
            guarded_publish({  # signed: alpha
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
            })
        except Exception:
            pass

        return {"action": "voted", "gate_id": gate_id, "yes": yes_count,
                "no": no_count, "needed": self.MAJORITY_THRESHOLD}

    def _elevate(self, gate_id: str, proposal: dict) -> dict:
        """Majority reached -- queue report for consolidated digest delivery."""
        proposal["status"] = "elevated"
        proposal["elevated_at"] = time.time()
        self._state["elevated"].append({
            "gate_id": gate_id,
            "report": proposal["report"],
            "proposer": proposal["proposer"],
            "voters": proposal["votes"],
            "fingerprint": proposal.get("fingerprint"),
            "elevated_at": proposal["elevated_at"],
            "delivery_type": self.DELIVERY_TYPE,
            "delivery_status": "pending",
        })
        del self._state["pending"][gate_id]
        self._state["stats"]["total_elevated"] = self._state["stats"].get("total_elevated", 0) + 1

        voters = [w for w, v in proposal["votes"].items() if v == "YES"]
        self._record_active_finding(gate_id, proposal, voters)
        self._queue_digest_delivery(gate_id, proposal, voters)
        self._save()
        try:
            guarded_publish({  # signed: alpha
                "sender": "convene-gate",
                "topic": "convene",
                "type": "gate-elevated-queued",
                "content": json.dumps({
                    "gate_id": gate_id,
                    "proposer": proposal["proposer"],
                    "voters": voters,
                    "delivery_type": self.DELIVERY_TYPE,
                    "deliver_after_s": self.DELIVERY_INTERVAL_S,
                    "report": proposal["report"][:300],
                }),
            })
        except Exception:
            pass

        flush_result = self._flush_due_digest()

        return {"action": "elevated", "gate_id": gate_id,
                "voters": voters, "delivered": flush_result.get("action") == "delivered",
                "delivery_type": self.DELIVERY_TYPE, "queued": True}

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
        self._flush_due_digest()
        return self._state.get("pending", {})

    def get_stats(self) -> dict:
        """Return gate statistics."""
        self._state = self._load()
        self._flush_due_digest()
        return self._state.get("stats", {})

    def expire_stale(self, max_age_s: int = 300):
        """Expire proposals older than max_age_s that haven't reached consensus."""
        self._state = self._load()
        self._flush_due_digest()
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
        guarded_publish({  # signed: alpha
            "sender": worker,
            "topic": "convene",
            "type": "vote",
            "content": json.dumps({
                "session_id": session_id,
                "proposal": proposal,
                "choice": choice,
            }),
        })
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

def _cmd_initiate(args):
    if not args.topic:
        print("--topic required for --initiate", file=sys.stderr)
        sys.exit(1)
    sid = initiate_convene(args.worker, args.topic, args.context, args.need)
    if sid:
        print(f"Session created: {sid}")
    else:
        print("Failed to create session", file=sys.stderr)
        sys.exit(1)


def _cmd_discover():
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


def _cmd_join(args):
    ok = join_session(args.worker, args.join)
    if ok:
        print(f"{args.worker} joined session {args.join}")
    else:
        print(f"Failed to join session {args.join}", file=sys.stderr)
        sys.exit(1)


def _print_session_detail(sid: str, s: dict) -> None:
    """Print detailed info for one convene session."""
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


def _cmd_status():
    sessions = discover_sessions()
    local = _load_sessions()
    all_sessions = {s.get("id"): s for s in sessions}
    for sid, s in local.items():
        if sid not in all_sessions:
            all_sessions[sid] = s
    if not all_sessions:
        print("No sessions")
        return
    for sid, s in all_sessions.items():
        _print_session_detail(sid, s)
    print()


def _cmd_vote(args):
    if not args.proposal:
        print("--proposal required for --vote-session", file=sys.stderr)
        sys.exit(1)
    ok = vote(args.vote_session, args.worker, args.proposal, args.choice)
    if ok:
        print(f"{args.worker} voted {args.choice} on '{args.proposal}'")
    else:
        sys.exit(1)


def _cmd_consensus(args):
    if not args.proposal:
        print("--proposal required for --consensus", file=sys.stderr)
        sys.exit(1)
    result = consensus(args.consensus, args.proposal)
    print(json.dumps(result, indent=2))


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
        _cmd_initiate(args)
    elif args.discover:
        _cmd_discover()
    elif args.join:
        _cmd_join(args)
    elif args.status:
        _cmd_status()
    elif args.orchestrate:
        task = args.task or f"Contribute to: {args.orchestrate}"
        result = orchestrate_convene(args.orchestrate, task, args.timeout)
        print(json.dumps(result, indent=2, default=str))
    elif args.vote_session:
        _cmd_vote(args)
    elif args.consensus:
        _cmd_consensus(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
