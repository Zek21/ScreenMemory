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
        'sender': 'alpha', 'topic': 'orchestrator',
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
    "convene_gate_proposal": 900,
    "convene_gate_vote": 86400,  # 24h -- same voter+gate_id is always a dupe
    "result_duplicate": 300,
    "daemon_health": 60,
    "knowledge_learning": 1800,
    "dead_alert": 120,
}

# Auto-penalty amount per blocked spam message
SPAM_PENALTY = 0.1


class SpamGuard:
    """Rate limiter and dedup guard for Skynet bus messages."""

    def __init__(self):
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
        normalized = content.lower().strip()
        normalized = re.sub(
            r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}[.\dzZ]*", "",
            normalized)
        # signed: alpha
        # Preserve worker suffix so different workers' proposals stay distinct
        normalized = re.sub(
            r"gate_\d+_(\w+)", r"GATE_\1", normalized)
        # signed: alpha
        normalized = re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            "UUID", normalized)
        normalized = re.sub(r"cycle[= ]*\d+", "CYCLE_N", normalized)
        normalized = re.sub(r"remaining=\d+h?", "REMAINING_N", normalized)
        normalized = re.sub(r"latency=[\d.]+ms", "LATENCY_N", normalized)
        # Normalize PID, port, and line numbers to catch near-duplicates
        normalized = re.sub(r"\bpid[= ]*\d+", "PID_N", normalized)
        normalized = re.sub(r"\bport[= ]*\d+", "PORT_N", normalized)
        normalized = re.sub(r"\bline[= ]*\d+", "LINE_N", normalized)
        normalized = re.sub(r"\bhwnd[= ]*\d+", "HWND_N", normalized)
        # signed: delta
        normalized = re.sub(r"\s+", " ", normalized).strip()

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

    def _check_spam_patterns(self, message: dict, fp: str) -> Optional[str]:
        """Check for specific spam patterns. Returns reason if spam, None if OK."""
        sender = str(message.get("sender", "")).lower()
        topic = str(message.get("topic", "")).lower()
        msg_type = str(message.get("type", "")).lower()
        content = str(message.get("content", ""))

        # 1. CONVENE gate-proposal with same issue_key within 900s
        if topic == "convene" and msg_type == "gate-proposal":
            if self.is_duplicate(fp, PATTERN_WINDOWS["convene_gate_proposal"]):
                return "spam_convene_gate_proposal: duplicate gate-proposal within 900s"

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
            # Record vote fingerprint
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

        # Check 3: Per-sender rate limiting
        rate_reason = self.is_rate_limited(sender)
        if rate_reason:
            self._handle_spam(sender, fp, rate_reason, message)
            return {"allowed": False, "reason": rate_reason,
                    "fingerprint": fp}

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
        """Deduct SPAM_PENALTY from sender's score."""
        try:
            sys.path.insert(0, str(ROOT))
            from tools.skynet_scoring import adjust_score
            adjust_score(sender, -SPAM_PENALTY, f"SPAM_BLOCKED: {reason}",
                         "spam_guard")
        except Exception:
            pass  # Scoring system unavailable -- still block the spam
        # signed: alpha

    def _log_spam(self, sender: str, fp: str, reason: str, message: dict):
        """Append to data/spam_log.json."""
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

            with open(LOG_FILE, "w", encoding="utf-8") as f:
                json.dump(log, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
        # signed: alpha

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
            urlopen(req, timeout=5)
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
    msg1 = {"sender": "alpha", "topic": "test", "type": "result",
            "content": "hello world"}
    fp1 = guard.fingerprint(msg1)
    fp2 = guard.fingerprint(msg1)
    check("fingerprint determinism", fp1 == fp2)
    check("fingerprint length", len(fp1) == 16)

    # Test 2: Different messages get different fingerprints
    msg2 = {"sender": "alpha", "topic": "test", "type": "result",
            "content": "goodbye world"}
    fp3 = guard.fingerprint(msg2)
    check("different messages != fingerprints", fp1 != fp3)

    # Test 3: Timestamp stripping
    msg3a = {"sender": "alpha", "topic": "test", "type": "info",
             "content": "event at 2026-03-11T18:00:00Z"}
    msg3b = {"sender": "alpha", "topic": "test", "type": "info",
             "content": "event at 2026-03-12T09:30:00Z"}
    check("timestamp normalization",
          guard.fingerprint(msg3a) == guard.fingerprint(msg3b))

    # Test 4: Gate ID normalization
    msg4a = {"sender": "delta", "topic": "convene", "type": "gate-proposal",
             "content": "proposal gate_123_delta test"}
    msg4b = {"sender": "delta", "topic": "convene", "type": "gate-proposal",
             "content": "proposal gate_456_delta test"}
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

    # Test 7: Pattern -- convene gate-proposal dupe
    guard.reset()
    gate_msg = {"sender": "delta", "topic": "convene",
                "type": "gate-proposal",
                "content": "proposal gate_789_delta some issue"}
    r1 = guard.publish_guarded(gate_msg)
    check("first gate-proposal allowed", r1["allowed"])
    r2 = guard.publish_guarded(gate_msg)
    check("duplicate gate-proposal blocked", not r2["allowed"])
    check("blocked reason contains convene",
          "convene" in r2.get("reason", ""))

    # Test 8: Pattern -- result dupe within 300s
    guard.reset()
    result_msg = {"sender": "alpha", "topic": "orchestrator",
                  "type": "result", "content": "task done successfully"}
    r3 = guard.publish_guarded(result_msg)
    check("first result allowed", r3["allowed"])
    r4 = guard.publish_guarded(result_msg)
    check("duplicate result blocked", not r4["allowed"])

    # Test 9: Pattern -- daemon_health within 60s
    guard.reset()
    health_msg = {"sender": "idle_monitor", "topic": "orchestrator",
                  "type": "daemon_health", "content": "healthy"}
    r5 = guard.publish_guarded(health_msg)
    check("first daemon_health allowed", r5["allowed"])
    r6 = guard.publish_guarded(health_msg)
    check("daemon_health within 60s blocked", not r6["allowed"])

    # Test 10: Stats tracking
    stats = guard.get_stats()
    check("stats tracks blocked count", stats["total_blocked"] >= 3)
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
    global _singleton_guard
    try:
        if _singleton_guard is None:
            _singleton_guard = SpamGuard()
        return _singleton_guard.publish_guarded(msg)
    except Exception:
        # Fallback: publish directly if guard is broken -- never drop messages
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
            'sender': 'alpha', 'topic': 'orchestrator',
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

        # Check 1: Pattern-specific spam
        pattern_reason = guard._check_spam_patterns(msg, fp)
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


if __name__ == "__main__":
    main()
