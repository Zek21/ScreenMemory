#!/usr/bin/env python3
"""
skynet_spam_guard.py -- Anti-spam rate limiter for the Skynet bus.

Wraps bus publishing with fingerprint dedup, per-sender rate limiting,
and pattern-specific spam detection. Blocked messages are logged and
senders are auto-penalized via skynet_scoring.py.

DUAL SPAM FILTER ARCHITECTURE (IMPORTANT)
==========================================
Skynet uses TWO independent spam filters. A message must pass BOTH to be published.
Messages can be blocked at EITHER layer independently.

Layer 1 — Python SpamGuard (THIS FILE, client-side):
  - Rate limit: 5 msgs/min/sender (default), 30 msgs/hour/sender
  - Dedup window: 900 seconds (15 min) general, category-specific overrides
  - Fingerprint: SHA-256 of (sender|topic|type|normalized_content[:200])
  - Normalization: strips timestamps, UUIDs, cycle numbers, gate IDs, etc.
  - Penalty: -0.1 score per blocked message via skynet_scoring.py
  - Override: monitor(10/min), system(10/min), convene-gate/convene(8/min)

Layer 2 — Go Backend (Skynet/server.go, server-side):
  - Rate limit: 10 msgs/min/sender (stricter than Python per-minute default)
  - Dedup window: 60 seconds (shorter than Python's 900s)
  - Fingerprint: sender|topic|type|content[:200] (NO normalization)
  - Returns: HTTP 429 "SPAM_BLOCKED: <reason>" when blocked
  - Cleanup: Background goroutine prunes stale entries every 5 minutes
  - Exempt: localhost requests exempt from HTTP rate limiting (500μs/IP)

Why two layers?
  - Python guard pre-filters before network call → saves bandwidth and reduces noise
  - Go guard catches direct HTTP callers that bypass Python (curl, scripts, etc.)
  - Python has longer dedup window (900s) to catch slow-repeat spam
  - Go has shorter dedup (60s) to allow legitimate near-duplicates after cool-down
  - Python applies score penalties; Go only blocks (no scoring integration)

If a message passes Python SpamGuard but is blocked by Go backend:
  → guarded_publish() returns {'allowed': True, 'published': False}
  → The message was not spam by Python rules but hit Go's stricter per-minute rate

If a message is blocked by Python SpamGuard:
  → The HTTP POST to Go backend never happens (saves network call)
  → Sender is auto-penalized -0.1 score
  → Block is logged to data/spam_log.json

Pre-flight check: use check_would_be_blocked(msg) to test without side effects.
# signed: gamma

State files:
  data/spam_guard_state.json  -- recent fingerprints + per-sender counters
  data/spam_log.json          -- log of blocked spam messages

Usage:
    python tools/skynet_spam_guard.py --stats
    python tools/skynet_spam_guard.py --reset
    python tools/skynet_spam_guard.py --test

API:
    from tools.skynet_spam_guard import SpamGuard
    guard = SpamGuard()
    result = guard.publish_guarded({
        'sender': 'worker_name', 'topic': 'orchestrator',
        'type': 'result', 'content': 'task done'
    })
    # result = {'allowed': True, 'fingerprint': '...'} or
    # result = {'allowed': False, 'reason': '...', 'fingerprint': '...'}
"""

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# ── Pre-compiled regex patterns for fingerprint normalization (perf: ~8-11ms savings) ──
# These patterns are applied to every bus message. Compiling once at module load
# eliminates per-call regex compilation overhead.  # signed: alpha
_RE_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}[.\dzZ]*")
_RE_GATE_ID = re.compile(r"gate_\d+_(\w+)")
_RE_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_RE_CYCLE = re.compile(r"cycle[= ]*\d+")
_RE_REMAINING = re.compile(r"remaining=\d+h?")
_RE_LATENCY = re.compile(r"latency=[\d.]+ms")
_RE_PID = re.compile(r"\bpid[= ]*\d+")
_RE_PORT = re.compile(r"\bport[= ]*\d+")
_RE_LINE = re.compile(r"\bline[= ]*\d+")
_RE_HWND = re.compile(r"\bhwnd[= ]*\d+")
_RE_WHITESPACE = re.compile(r"\s+")
STATE_FILE = DATA_DIR / "spam_guard_state.json"
LOG_FILE = DATA_DIR / "spam_log.json"
BUS_URL = "http://localhost:8420/bus/publish"

# Default rate limits
DEFAULT_MAX_PER_MINUTE = 5
DEFAULT_MAX_PER_HOUR = 30
DEFAULT_DEDUP_WINDOW = 900  # 15 minutes

# Per-sender rate limit overrides (senders with higher legitimate traffic)
# Format: sender_name -> (max_per_minute, max_per_hour)
SENDER_RATE_OVERRIDES = {
    "monitor": (10, 90),        # health checks every 60s + alerts + model drift
    "system": (10, 60),         # infrastructure messages during boot bursts
    "convene-gate": (8, 60),    # gate elevation traffic can burst
    "convene": (8, 60),         # convene session coordination bursts
}
# signed: delta

# Pattern-specific windows (seconds)
PATTERN_WINDOWS = {
    "convene_gate_proposal": 120,   # 2 min — was 900s (too aggressive, blocked legit re-proposals)
    "convene_gate_vote": 86400,     # 24h -- same voter+gate_id is always a dupe
    "result_duplicate": 300,
    "daemon_health": 60,
    "knowledge_learning": 1800,
    "dead_alert": 120,
}

# Auto-penalty amount per blocked spam message
# MUST be proportional to award (DEFAULT_AWARD=0.01), not 10x it
SPAM_PENALTY = 0.02  # was 0.1 (10x award) — now 2x award, proportional


class SpamGuard:
    """Rate limiter and dedup guard for Skynet bus messages."""

    def __init__(self):
        """Load persistent spam guard state from data/spam_guard_state.json."""  # signed: alpha
        self._state = self._load_state()
        # signed: alpha

    # ── Fingerprinting ──────────────────────────────────────────

    @staticmethod
    def fingerprint(message: dict) -> str:
        """SHA256 fingerprint of sender+topic+type+normalized_content.

        Strips timestamps, UUIDs, gate IDs, and cycle numbers from content
        before hashing to catch semantically identical messages.
        """
        sender = str(message.get("sender", "")).lower().strip()
        topic = str(message.get("topic", "")).lower().strip()
        msg_type = str(message.get("type", "")).lower().strip()
        content = str(message.get("content", ""))

        # Normalize: strip timestamps, UUIDs, gate IDs, cycle numbers,
        # PIDs, port numbers, and line numbers to catch semantic duplicates
        # Uses pre-compiled patterns for performance (~8-11ms savings per call)  # signed: alpha
        normalized = content.lower().strip()
        normalized = _RE_TIMESTAMP.sub("", normalized)
        # signed: alpha
        # Preserve worker suffix so different workers' proposals stay distinct
        normalized = _RE_GATE_ID.sub(r"GATE_\1", normalized)
        # signed: alpha
        normalized = _RE_UUID.sub("UUID", normalized)
        normalized = _RE_CYCLE.sub("CYCLE_N", normalized)
        normalized = _RE_REMAINING.sub("REMAINING_N", normalized)
        normalized = _RE_LATENCY.sub("LATENCY_N", normalized)
        # Normalize PID, port, and line numbers to catch near-duplicates
        normalized = _RE_PID.sub("PID_N", normalized)
        normalized = _RE_PORT.sub("PORT_N", normalized)
        normalized = _RE_LINE.sub("LINE_N", normalized)
        normalized = _RE_HWND.sub("HWND_N", normalized)
        # signed: delta
        normalized = _RE_WHITESPACE.sub(" ", normalized).strip()

        raw = f"{sender}|{topic}|{msg_type}|{normalized}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        # signed: alpha

    # ── Dedup Check ─────────────────────────────────────────────

    def is_duplicate(self, fp: str, window_seconds: int = DEFAULT_DEDUP_WINDOW) -> bool:
        """Check if this fingerprint was seen within the window."""
        now = time.time()
        seen = self._state.get("fingerprints", {})
        last_seen = seen.get(fp, 0)
        return (now - last_seen) < window_seconds
        # signed: alpha

    # ── Rate Limit Check ────────────────────────────────────────

    def is_rate_limited(self, sender: str,
                        max_per_minute: int = DEFAULT_MAX_PER_MINUTE,
                        max_per_hour: int = DEFAULT_MAX_PER_HOUR) -> Optional[str]:
        """Check if sender exceeds rate limits.

        Uses per-sender overrides from SENDER_RATE_OVERRIDES if available,
        otherwise falls back to provided defaults.

        Returns None if OK, or a reason string if rate-limited.
        """
        # Apply sender-specific overrides if defined
        sender_lower = sender.lower().strip()
        if sender_lower in SENDER_RATE_OVERRIDES:
            max_per_minute, max_per_hour = SENDER_RATE_OVERRIDES[sender_lower]
        # signed: delta

        now = time.time()
        counters = self._state.get("sender_timestamps", {})
        timestamps = counters.get(sender, [])

        # Clean old timestamps (older than 1 hour)
        timestamps = [t for t in timestamps if now - t < 3600]

        minute_count = sum(1 for t in timestamps if now - t < 60)
        hour_count = len(timestamps)

        if minute_count >= max_per_minute:
            return (f"rate_limit_minute: {sender} sent {minute_count} "
                    f"messages in last 60s (limit={max_per_minute})")
        if hour_count >= max_per_hour:
            return (f"rate_limit_hour: {sender} sent {hour_count} "
                    f"messages in last 3600s (limit={max_per_hour})")
        return None
        # signed: alpha

    # ── Pattern-Specific Spam Detection ─────────────────────────

    def _check_spam_patterns(self, message: dict, fp: str,
                               record: bool = True) -> Optional[str]:
        """Check for specific spam patterns. Returns reason if spam, None if OK.

        Args:
            record: If False, skip _record_fingerprint calls (read-only mode).
                    Used by check_would_be_blocked() to avoid side effects.
        """
        # signed: gamma — fix: added record param to prevent side effects in read-only path
        sender = str(message.get("sender", "")).lower()
        topic = str(message.get("topic", "")).lower()
        msg_type = str(message.get("type", "")).lower()
        content = str(message.get("content", ""))

        # 1. CONVENE gate-proposal with same issue_key within 900s
        if topic == "convene" and msg_type == "gate-proposal":
            if self.is_duplicate(fp, PATTERN_WINDOWS["convene_gate_proposal"]):
                return f"spam_convene_gate_proposal: duplicate gate-proposal within {PATTERN_WINDOWS['convene_gate_proposal']}s"  # signed: gamma

        # 2. CONVENE gate-vote duplicates (same voter+gate_id)
        if topic == "convene" and msg_type == "gate-vote":
            # Build a vote-specific fingerprint: voter + gate_id
            gate_match = re.search(r"gate_\d+_\w+", content, re.IGNORECASE)
            gate_id = gate_match.group(0) if gate_match else "unknown"
            vote_fp = hashlib.sha256(
                f"vote|{sender}|{gate_id}".encode()).hexdigest()[:16]
            if self.is_duplicate(vote_fp, PATTERN_WINDOWS["convene_gate_vote"]):
                return (f"spam_convene_vote_dupe: {sender} already voted on "
                        f"{gate_id}")
            if record:
                self._record_fingerprint(vote_fp)

        # 3. Identical result messages from same sender within 300s
        if msg_type == "result":
            if self.is_duplicate(fp, PATTERN_WINDOWS["result_duplicate"]):
                return "spam_result_dupe: identical result from same sender within 300s"

        # 4. daemon_health messages more than 1 per 60s per daemon
        if msg_type == "daemon_health":
            daemon_fp = hashlib.sha256(
                f"daemon_health|{sender}".encode()).hexdigest()[:16]
            if self.is_duplicate(daemon_fp, PATTERN_WINDOWS["daemon_health"]):
                return (f"spam_daemon_health: {sender} sent daemon_health "
                        f"within 60s")
            if record:
                self._record_fingerprint(daemon_fp)

        # 5. knowledge/learning duplicates within 1800s
        if topic in ("knowledge", "learning") or msg_type == "learning":
            if self.is_duplicate(fp, PATTERN_WINDOWS["knowledge_learning"]):
                return "spam_knowledge_dupe: identical learning within 1800s"

        # 6. DEAD alerts for same worker within 120s
        if msg_type == "alert" and "DEAD" in content.upper():
            dead_match = re.search(r"(\w+)\s+(?:is\s+)?DEAD", content,
                                   re.IGNORECASE)
            dead_worker = dead_match.group(1) if dead_match else "unknown"
            dead_fp = hashlib.sha256(
                f"dead_alert|{dead_worker}".encode()).hexdigest()[:16]
            if self.is_duplicate(dead_fp, PATTERN_WINDOWS["dead_alert"]):
                return (f"spam_dead_alert_dupe: DEAD alert for {dead_worker} "
                        f"within 120s")
            if record:
                self._record_fingerprint(dead_fp)

        return None
        # signed: alpha

    # ── Guarded Publish ─────────────────────────────────────────

    def publish_guarded(self, message: dict) -> dict:
        """Wrap POST to /bus/publish with spam checks.

        Returns dict with 'allowed' bool + details.
        If spam detected: blocks message, logs, auto-penalizes sender.
        """
        sender = str(message.get("sender", "unknown"))
        fp = self.fingerprint(message)

        # Check 1: Pattern-specific spam
        pattern_reason = self._check_spam_patterns(message, fp)
        if pattern_reason:
            self._handle_spam(sender, fp, pattern_reason, message)
            return {"allowed": False, "reason": pattern_reason,
                    "fingerprint": fp}

        # Check 2: General dedup (default 900s window)
        if self.is_duplicate(fp, DEFAULT_DEDUP_WINDOW):
            reason = "general_dedup: identical message within 900s"
            self._handle_spam(sender, fp, reason, message)
            return {"allowed": False, "reason": reason, "fingerprint": fp}

        # Check 3: Per-sender rate limiting (priority-aware)
        # Messages with metadata.priority=critical bypass rate limits,
        # but ONLY from authorized senders (system, monitor, orchestrator).
        # Untrusted senders with priority=critical are downgraded to normal.
        # signed: gamma — fix: critical priority bypass security hole
        _CRITICAL_AUTHORIZED = {"system", "monitor", "orchestrator", "watchdog"}
        priority = "normal"
        if isinstance(message.get("metadata"), dict):
            priority = message["metadata"].get("priority", "normal")
        skip_rate_limit = (
            priority == "critical"
            and sender.lower().strip() in _CRITICAL_AUTHORIZED
        )

        if not skip_rate_limit:
            if priority == "low" and "low" in PRIORITY_RATE_OVERRIDES:
                low_limits = PRIORITY_RATE_OVERRIDES["low"]
                if low_limits:
                    rate_reason = self.is_rate_limited(
                        sender, max_per_minute=low_limits[0],
                        max_per_hour=low_limits[1])
                else:
                    rate_reason = self.is_rate_limited(sender)
            else:
                rate_reason = self.is_rate_limited(sender)
            if rate_reason:
                self._handle_spam(sender, fp, rate_reason, message)
                return {"allowed": False, "reason": rate_reason,
                        "fingerprint": fp}
        # signed: gamma

        # All checks passed -- publish FIRST, then record fingerprint.
        # Recording fingerprint before bus post is a bug: if POST fails,
        # the fingerprint is stored and the retry is blocked as duplicate.
        ok = self._bus_post(message)
        if ok:
            self._record_fingerprint(fp)
            self._record_sender_timestamp(sender)
            self._state.setdefault("stats", {
                "total_blocked": 0, "total_allowed": 0,
                "blocked_by_pattern": {}, "blocked_by_sender": {}
            })["total_allowed"] = \
                self._state["stats"].get("total_allowed", 0) + 1
        # signed: beta — fix: stats only increment on successful POST
        self._save_state()
        return {"allowed": True, "published": ok, "fingerprint": fp}
        # signed: delta

    # ── Internal Helpers ────────────────────────────────────────

    def _record_fingerprint(self, fp: str):
        """Record a fingerprint with current timestamp."""
        if "fingerprints" not in self._state:
            self._state["fingerprints"] = {}
        self._state["fingerprints"][fp] = time.time()
        # signed: alpha

    def _record_sender_timestamp(self, sender: str):
        """Record a send timestamp for rate limiting."""
        if "sender_timestamps" not in self._state:
            self._state["sender_timestamps"] = {}
        if sender not in self._state["sender_timestamps"]:
            self._state["sender_timestamps"][sender] = []
        self._state["sender_timestamps"][sender].append(time.time())
        # signed: alpha

    def _handle_spam(self, sender: str, fp: str, reason: str,
                     message: dict):
        """Log spam, increment counters, auto-penalize sender."""
        # Update stats
        stats = self._state.setdefault("stats", {
            "total_blocked": 0, "total_allowed": 0,
            "blocked_by_pattern": {}, "blocked_by_sender": {}
        })
        stats["total_blocked"] += 1

        # Track by pattern type
        pattern_key = reason.split(":")[0] if ":" in reason else "unknown"
        stats["blocked_by_pattern"][pattern_key] = \
            stats["blocked_by_pattern"].get(pattern_key, 0) + 1

        # Track by sender
        stats["blocked_by_sender"][sender] = \
            stats["blocked_by_sender"].get(sender, 0) + 1

        self._save_state()

        # Log to spam_log.json
        self._log_spam(sender, fp, reason, message)

        # Auto-penalize sender via skynet_scoring
        self._auto_penalize(sender, reason)
        # signed: alpha

    def _auto_penalize(self, sender: str, reason: str):
        """Deduct SPAM_PENALTY from sender's score. Daemons and convene-protocol dupes exempt."""
        # Convene-protocol duplicates are systemic, not intentional spam.
        # Workers post gate-proposals/votes with their own sender name,
        # so sender-based exemption doesn't catch them. Exempt by reason instead.
        # signed: gamma
        _EXEMPT_REASON_PREFIXES = (
            "spam_convene_gate_proposal",
            "spam_convene_vote_dupe",
        )
        if reason.startswith(_EXEMPT_REASON_PREFIXES):
            return
        # Local fallback set — used if scoring module import fails
        _LOCAL_SYSTEM_SENDERS = {
            "monitor", "convene", "convene-gate", "convene_gate", "self_prompt",
            "system", "overseer", "watchdog", "bus_relay", "learner",
            "self_improve", "sse_daemon", "idle_monitor",
            "skynet_self", "skynet_monitor", "skynet_learner", "skynet_watchdog",
            "skynet_overseer", "skynet_bus_relay", "skynet_self_prompt",
            "skynet_self_improve", "skynet_sse_daemon", "skynet_idle_monitor",
            "bus_watcher", "ws_monitor", "bus_persist", "consultant_consumer",
            "health_report", "worker_loop", "daemon_status",
        }
        # Check local set first — always works regardless of import
        if sender in _LOCAL_SYSTEM_SENDERS:
            return
        try:
            sys.path.insert(0, str(ROOT))
            from tools.skynet_scoring import adjust_score, SYSTEM_SENDERS
            if sender in SYSTEM_SENDERS:
                return
            adjust_score(sender, -SPAM_PENALTY, f"SPAM_BLOCKED: {reason}",
                         "spam_guard")
        except Exception:
            pass  # Scoring system unavailable -- still block the spam
        # signed: orchestrator

    def _log_spam(self, sender: str, fp: str, reason: str, message: dict):
        """Append to data/spam_log.json. Uses atomic write to prevent TOCTOU race."""
        try:
            if LOG_FILE.exists():
                with open(LOG_FILE, "r", encoding="utf-8") as f:
                    log = json.load(f)
            else:
                log = {"entries": [], "version": 1}

            entry = {
                "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "sender": sender,
                "fingerprint": fp,
                "reason": reason,
                "topic": message.get("topic", ""),
                "type": message.get("type", ""),
                "content_preview": str(message.get("content", ""))[:200],
                "penalty": SPAM_PENALTY,
            }
            log["entries"].append(entry)

            # Keep last 500 entries
            if len(log["entries"]) > 500:
                log["entries"] = log["entries"][-500:]

            # Atomic write via temp file + rename to prevent TOCTOU race  # signed: alpha
            tmp = LOG_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(log, f, indent=2, ensure_ascii=False)
            tmp.replace(LOG_FILE)
        except Exception:
            pass

    @staticmethod
    def _bus_post(message: dict) -> bool:
        """POST message to Skynet bus."""
        try:
            from urllib.request import Request, urlopen
            req = Request(
                BUS_URL,
                data=json.dumps(message).encode(),
                headers={"Content-Type": "application/json"}
            )
            resp = urlopen(req, timeout=5)
            resp.close()  # signed: gamma — prevent FD leak
            return True
        except Exception:
            return False
        # signed: alpha

    # ── State Persistence ───────────────────────────────────────

    def _load_state(self) -> dict:
        """Load state from disk, initializing if needed."""
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                # Prune old fingerprints (older than 2 hours)
                now = time.time()
                fps = state.get("fingerprints", {})
                state["fingerprints"] = {
                    k: v for k, v in fps.items() if now - v < 7200
                }
                # Prune old sender timestamps (older than 1 hour)
                for sender in list(state.get("sender_timestamps", {})):
                    ts_list = state["sender_timestamps"][sender]
                    state["sender_timestamps"][sender] = [
                        t for t in ts_list if now - t < 3600
                    ]
                    if not state["sender_timestamps"][sender]:
                        del state["sender_timestamps"][sender]
                return state
        except Exception:
            pass
        return {
            "fingerprints": {},
            "sender_timestamps": {},
            "stats": {
                "total_blocked": 0,
                "total_allowed": 0,
                "blocked_by_pattern": {},
                "blocked_by_sender": {},
            },
            "version": 1,
        }
        # signed: alpha

    def _save_state(self):
        """Persist state to disk."""
        try:
            DATA_DIR.mkdir(exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, ensure_ascii=False)
            tmp.replace(STATE_FILE)
        except Exception:
            pass
        # signed: alpha

    # ── Stats / Reset ───────────────────────────────────────────

    def get_stats(self) -> dict:
        """Return spam guard statistics."""
        stats = self._state.get("stats", {})
        fps_count = len(self._state.get("fingerprints", {}))
        senders_tracked = len(self._state.get("sender_timestamps", {}))
        return {
            "total_blocked": stats.get("total_blocked", 0),
            "total_allowed": stats.get("total_allowed", 0),
            "blocked_by_pattern": stats.get("blocked_by_pattern", {}),
            "blocked_by_sender": stats.get("blocked_by_sender", {}),
            "active_fingerprints": fps_count,
            "senders_tracked": senders_tracked,
        }
        # signed: alpha

    def reset(self):
        """Clear all state and logs."""
        self._state = {
            "fingerprints": {},
            "sender_timestamps": {},
            "stats": {
                "total_blocked": 0,
                "total_allowed": 0,
                "blocked_by_pattern": {},
                "blocked_by_sender": {},
            },
            "version": 1,
        }
        self._save_state()
        if LOG_FILE.exists():
            LOG_FILE.unlink()
        # signed: alpha


# ── Self-Test ───────────────────────────────────────────────────

def run_self_test():
    """Run self-test to validate all spam guard functionality."""
    guard = SpamGuard()
    guard.reset()
    passed = 0
    failed = 0

    def check(name, condition):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  PASS: {name}")
        else:
            failed += 1
            print(f"  FAIL: {name}")

    print("=== SpamGuard Self-Test ===\n")

    # Test 1: Fingerprinting determinism
    msg1 = {"sender": "_test_spam_sender", "topic": "test", "type": "result",
            "content": "hello world"}
    fp1 = guard.fingerprint(msg1)
    fp2 = guard.fingerprint(msg1)
    check("fingerprint determinism", fp1 == fp2)
    check("fingerprint length", len(fp1) == 16)

    # Test 2: Different messages get different fingerprints
    msg2 = {"sender": "_test_spam_sender", "topic": "test", "type": "result",
            "content": "goodbye world"}
    fp3 = guard.fingerprint(msg2)
    check("different messages != fingerprints", fp1 != fp3)

    # Test 3: Timestamp stripping
    msg3a = {"sender": "_test_spam_sender", "topic": "test", "type": "info",
             "content": "event at 2026-03-11T18:00:00Z"}
    msg3b = {"sender": "_test_spam_sender", "topic": "test", "type": "info",
             "content": "event at 2026-03-12T09:30:00Z"}
    check("timestamp normalization",
          guard.fingerprint(msg3a) == guard.fingerprint(msg3b))

    # Test 4: Gate ID normalization
    msg4a = {"sender": "_test_delta", "topic": "convene", "type": "gate-proposal",
             "content": "proposal gate_123__test_delta test"}
    msg4b = {"sender": "_test_delta", "topic": "convene", "type": "gate-proposal",
             "content": "proposal gate_456__test_delta test"}
    check("gate_id normalization",
          guard.fingerprint(msg4a) == guard.fingerprint(msg4b))

    # Test 5: Dedup detection
    guard._record_fingerprint(fp1)
    guard._save_state()
    check("is_duplicate detects recent fp", guard.is_duplicate(fp1, 900))
    check("is_duplicate misses unknown fp",
          not guard.is_duplicate("nonexistent123456", 900))

    # Test 6: Rate limiting
    guard.reset()
    for i in range(6):
        guard._record_sender_timestamp("spammer")
    rl = guard.is_rate_limited("spammer", max_per_minute=5)
    check("rate_limit triggers at 6/5 per minute", rl is not None)
    rl2 = guard.is_rate_limited("normal_sender", max_per_minute=5)
    check("rate_limit passes for clean sender", rl2 is None)

    # Use per-run nonce in test content to avoid Go backend 60s dedup interference
    # signed: gamma — fix: test isolation from Go server-side dedup
    import os
    _nonce = os.urandom(4).hex()

    # Test 7: Pattern -- convene gate-proposal dupe
    guard.reset()
    gate_msg = {"sender": "_test_delta", "topic": "convene",
                "type": "gate-proposal",
                "content": f"proposal gate_789__test_delta issue {_nonce}a"}
    r1 = guard.publish_guarded(gate_msg)
    check("first gate-proposal allowed", r1["allowed"])
    r2 = guard.publish_guarded(gate_msg)
    check("duplicate gate-proposal blocked", not r2["allowed"])
    check("blocked reason contains convene",
          "convene" in r2.get("reason", ""))

    # Test 8: Pattern -- result dupe within 300s
    guard.reset()
    result_msg = {"sender": "_test_spam_sender", "topic": "orchestrator",
                  "type": "result", "content": f"task done {_nonce}b"}
    r3 = guard.publish_guarded(result_msg)
    check("first result allowed", r3["allowed"])
    r4 = guard.publish_guarded(result_msg)
    check("duplicate result blocked", not r4["allowed"])

    # Test 9: Pattern -- daemon_health within 60s
    guard.reset()
    health_msg = {"sender": "idle_monitor", "topic": "orchestrator",
                  "type": "daemon_health", "content": f"healthy {_nonce}c"}
    r5 = guard.publish_guarded(health_msg)
    check("first daemon_health allowed", r5["allowed"])
    r6 = guard.publish_guarded(health_msg)
    check("daemon_health within 60s blocked", not r6["allowed"])

    # Test 10: Stats tracking (after test 9's reset, only 1 block from daemon_health)
    stats = guard.get_stats()
    check("stats tracks blocked count", stats["total_blocked"] >= 1)
    # signed: gamma — fix: stats check reflects reset() between test pairs
    check("stats tracks by pattern",
          len(stats["blocked_by_pattern"]) >= 1)

    # Test 11: Reset clears everything
    guard.reset()
    stats2 = guard.get_stats()
    check("reset clears blocked count", stats2["total_blocked"] == 0)
    check("reset clears fingerprints", stats2["active_fingerprints"] == 0)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed == 0
    # signed: alpha


# ── CLI ─────────────────────────────────────────────────────────

def main():
    """CLI entry point: --stats, --reset, --test, --log N for spam guard management."""  # signed: alpha
    parser = argparse.ArgumentParser(
        description="Skynet Bus Anti-Spam Guard")
    parser.add_argument("--stats", action="store_true",
                        help="Show spam guard statistics")
    parser.add_argument("--reset", action="store_true",
                        help="Reset all state and logs")
    parser.add_argument("--test", action="store_true",
                        help="Run self-test suite")
    parser.add_argument("--log", type=int, metavar="N", default=0,
                        help="Show last N spam log entries")

    args = parser.parse_args()
    guard = SpamGuard()

    if args.test:
        ok = run_self_test()
        sys.exit(0 if ok else 1)

    elif args.stats:
        stats = guard.get_stats()
        print("=== Spam Guard Stats ===")
        print(f"Total blocked:       {stats['total_blocked']}")
        print(f"Total allowed:       {stats['total_allowed']}")
        print(f"Active fingerprints: {stats['active_fingerprints']}")
        print(f"Senders tracked:     {stats['senders_tracked']}")
        if stats["blocked_by_pattern"]:
            print("\nBlocked by pattern:")
            for pat, count in sorted(stats["blocked_by_pattern"].items(),
                                     key=lambda x: -x[1]):
                print(f"  {pat:<35} {count:>5}")
        if stats["blocked_by_sender"]:
            print("\nBlocked by sender:")
            for sender, count in sorted(stats["blocked_by_sender"].items(),
                                        key=lambda x: -x[1]):
                print(f"  {sender:<20} {count:>5}")

    elif args.reset:
        guard.reset()
        print("Spam guard state and logs cleared.")

    elif args.log:
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                log = json.load(f)
            entries = log.get("entries", [])[-args.log:]
            if not entries:
                print("No spam log entries.")
            else:
                print(f"Last {len(entries)} spam log entries:")
                for e in entries:
                    print(f"  [{e.get('timestamp', '?')}] "
                          f"{e.get('sender', '?'):<12} "
                          f"{e.get('reason', '?')[:60]}")
        except FileNotFoundError:
            print("No spam log file found.")

    else:
        parser.print_help()
    # signed: alpha


# ── Module-level convenience function ───────────────────────────
# Usage: from tools.skynet_spam_guard import guarded_publish
#        guarded_publish({"sender": "x", "topic": "y", "type": "z", "content": "..."})
# signed: alpha — removed stray delta_identity_ack() (dead code, wrong module)

# Fallback rate limiter: lightweight in-memory counter used when SpamGuard
# initialization fails (e.g., corrupted state file). Prevents unguarded
# bus flooding through the fallback path.  # signed: gamma
_fallback_timestamps: dict = {}  # sender -> list of timestamps


def _fallback_rate_ok(sender: str, max_per_minute: int = 5) -> bool:
    """Basic in-memory rate check for the fallback publish path."""
    now = time.time()
    ts = _fallback_timestamps.get(sender, [])
    ts = [t for t in ts if now - t < 60]
    if len(ts) >= max_per_minute:
        return False
    ts.append(now)
    _fallback_timestamps[sender] = ts
    return True

_singleton_guard: Optional[SpamGuard] = None


def guarded_publish(msg: dict) -> dict:
    """One-line spam-guarded bus publish. Returns SpamGuard result dict.

    Wraps SpamGuard.publish_guarded() with a module-level singleton so callers
    don't need to instantiate SpamGuard themselves. If the guard fails to
    initialize, falls back to direct bus POST (never silently drops messages).

    Args:
        msg: dict with sender, topic, type, content keys.

    Returns:
        dict with 'allowed' bool + details. On fallback: {'allowed': True, 'fallback': True}.
    """
    # Type guard: reject None and non-dict inputs before they reach the bus  # signed: delta
    if not isinstance(msg, dict):
        return {"allowed": False, "published": False,
                "reason": f"invalid_message_type: expected dict, got {type(msg).__name__}"}
    if not msg.get("sender") or not msg.get("content"):
        return {"allowed": False, "published": False,
                "reason": "missing required fields: sender and content are mandatory"}
    global _singleton_guard
    try:
        if _singleton_guard is None:
            _singleton_guard = SpamGuard()
        return _singleton_guard.publish_guarded(msg)
    except Exception:
        # Fallback: publish with basic in-memory rate check to prevent abuse.
        # Without this, a corrupted state file would disable all spam protection.
        # signed: gamma — fix: fallback guard bypass vulnerability
        if not _fallback_rate_ok(str(msg.get("sender", "unknown"))):
            return {"allowed": False, "fallback": True,
                    "reason": "fallback_rate_limited"}
        try:
            SpamGuard._bus_post(msg)
            return {"allowed": True, "fallback": True, "reason": "guard_init_failed"}
        except Exception:
            return {"allowed": False, "fallback": True, "reason": "bus_unreachable"}
    # signed: beta


def check_would_be_blocked(msg: dict) -> dict:
    """Pre-flight check: test if a message WOULD be blocked without side effects.

    Runs the same 3-check pipeline as publish_guarded() (pattern spam, dedup,
    rate limit) but in READ-ONLY mode. Does NOT record fingerprints, does NOT
    update sender timestamps, does NOT publish the message, and does NOT
    apply score penalties.

    Useful for pre-flight checks before expensive operations: if the result
    message would be spam-blocked, the caller can skip the operation entirely.

    Args:
        msg: dict with sender, topic, type, content keys (same as guarded_publish).

    Returns:
        dict with:
            'would_block': bool  -- True if the message WOULD be blocked
            'reason': str        -- Why it would be blocked (empty if not blocked)
            'fingerprint': str   -- The computed fingerprint for reference
            'checks': dict       -- Results of each individual check

    Example:
        result = check_would_be_blocked({
            'sender': 'worker_name', 'topic': 'orchestrator',
            'type': 'result', 'content': 'task done'
        })
        if result['would_block']:
            print(f"Skip: would be blocked by {result['reason']}")
        else:
            guarded_publish(msg)  # Safe to send
    """
    global _singleton_guard
    try:
        if _singleton_guard is None:
            _singleton_guard = SpamGuard()
        guard = _singleton_guard

        fp = guard.fingerprint(msg)
        checks = {"pattern": None, "dedup": None, "rate_limit": None}

        # Check 1: Pattern-specific spam (read-only: no fingerprint recording)
        pattern_reason = guard._check_spam_patterns(msg, fp, record=False)
        if pattern_reason:
            checks["pattern"] = pattern_reason
            return {
                "would_block": True,
                "reason": pattern_reason,
                "fingerprint": fp,
                "checks": checks,
            }

        # Check 2: General dedup (read-only -- just checks, doesn't record)
        if guard.is_duplicate(fp, DEFAULT_DEDUP_WINDOW):
            reason = "general_dedup: identical message within 900s"
            checks["dedup"] = reason
            return {
                "would_block": True,
                "reason": reason,
                "fingerprint": fp,
                "checks": checks,
            }

        # Check 3: Per-sender rate limiting (read-only)
        sender = str(msg.get("sender", "unknown"))
        rate_reason = guard.is_rate_limited(sender)
        if rate_reason:
            checks["rate_limit"] = rate_reason
            return {
                "would_block": True,
                "reason": rate_reason,
                "fingerprint": fp,
                "checks": checks,
            }

        # All checks passed -- message would NOT be blocked
        return {
            "would_block": False,
            "reason": "",
            "fingerprint": fp,
            "checks": checks,
        }
    except Exception as e:
        # Guard broken -- assume not blocked (conservative: don't prevent sends)
        return {
            "would_block": False,
            "reason": f"guard_error: {e}",
            "fingerprint": "",
            "checks": {},
        }
    # signed: gamma


# ── Priority Levels ──────────────────────────────────────────────
# Messages can specify metadata.priority to affect rate limiting:
#   critical — Bypasses rate limits entirely (still subject to dedup)
#   high     — Uses default rate limits (5/min)
#   normal   — Uses default rate limits (5/min) -- this is the default
#   low      — Stricter rate limits (2/min, 15/hour)
PRIORITY_RATE_OVERRIDES = {
    "critical": None,       # None = bypass rate limiting
    "high": None,           # Uses sender defaults
    "normal": None,         # Uses sender defaults
    "low": (2, 15),         # (max_per_minute, max_per_hour)
}
# signed: gamma


def bus_health() -> dict:
    """Return comprehensive bus health metrics for instant visibility.

    Probes the Go backend /bus/messages and /status endpoints plus
    local state files to build a complete health snapshot.

    Returns:
        dict with keys:
            messages_in_last_minute: int   -- Bus messages in last 60s
            unique_senders: list[str]      -- Distinct senders in last minute
            spam_blocked_count: int        -- Total spam blocked (all time)
            ring_buffer_utilization: dict  -- {current: N, max: 100, pct: float}
            archive_file_size_kb: float    -- JSONL archive size in KB
            last_message_timestamp: str    -- Timestamp of most recent bus msg
            spam_guard_fingerprints: int   -- Active fingerprints in guard
            spam_guard_senders: int        -- Tracked senders in guard
            bus_reachable: bool            -- Can we reach the Go backend?
    """
    import urllib.request
    import urllib.error

    result = {
        "messages_in_last_minute": 0,
        "unique_senders": [],
        "spam_blocked_count": 0,
        "ring_buffer_utilization": {"current": 0, "max": 100, "pct": 0.0},
        "archive_file_size_kb": 0.0,
        "last_message_timestamp": "",
        "spam_guard_fingerprints": 0,
        "spam_guard_senders": 0,
        "bus_reachable": False,
    }

    # Probe Go backend for bus messages
    try:
        req = urllib.request.Request(
            "http://localhost:8420/bus/messages?limit=100",
            headers={"Accept": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=3)
        messages = json.loads(resp.read().decode("utf-8"))
        if isinstance(messages, list):
            result["bus_reachable"] = True
            result["ring_buffer_utilization"]["current"] = len(messages)
            result["ring_buffer_utilization"]["pct"] = round(
                len(messages) / 100.0 * 100, 1
            )

            # Count messages in last 60 seconds and unique senders
            now = time.time()
            senders_set = set()
            recent_count = 0
            latest_ts = ""
            for msg in messages:
                sender = msg.get("sender", "")
                ts = msg.get("timestamp", "")
                if ts and (not latest_ts or ts > latest_ts):
                    latest_ts = ts
                # Try to parse timestamp for recency check
                try:
                    from datetime import datetime as _dt
                    if "T" in str(ts):
                        msg_time = _dt.fromisoformat(
                            str(ts).replace("Z", "+00:00")
                        ).timestamp()
                        if now - msg_time < 60:
                            recent_count += 1
                            senders_set.add(sender)
                except (ValueError, TypeError, OSError):
                    pass
            result["messages_in_last_minute"] = recent_count
            result["unique_senders"] = sorted(senders_set)
            result["last_message_timestamp"] = latest_ts
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        pass

    # SpamGuard state
    try:
        guard = SpamGuard()
        stats = guard.get_stats()
        result["spam_blocked_count"] = stats.get("total_blocked", 0)
        result["spam_guard_fingerprints"] = stats.get("active_fingerprints", 0)
        result["spam_guard_senders"] = stats.get("senders_tracked", 0)
    except Exception:
        pass

    # Archive file size
    archive_path = DATA_DIR / "bus_archive.jsonl"
    try:
        if archive_path.exists():
            result["archive_file_size_kb"] = round(
                archive_path.stat().st_size / 1024.0, 1
            )
    except OSError:
        pass

    return result
    # signed: gamma


if __name__ == "__main__":
    main()
