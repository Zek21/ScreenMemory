#!/usr/bin/env python3
"""Core tests for skynet_spam_guard.py — fingerprinting, dedup, rate limiting, guarded_publish.

Tests SpamGuard logic WITHOUT requiring a running Skynet backend.
All bus POST calls are mocked.
# signed: gamma
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def guard(tmp_path):
    """Create a SpamGuard with isolated state file (no side effects)."""
    state_file = tmp_path / "spam_guard_state.json"
    state_file.write_text("{}", encoding="utf-8")
    log_file = tmp_path / "spam_log.json"
    log_file.write_text("[]", encoding="utf-8")

    with patch("tools.skynet_spam_guard.STATE_FILE", state_file), \
         patch("tools.skynet_spam_guard.LOG_FILE", log_file):
        from tools.skynet_spam_guard import SpamGuard
        g = SpamGuard()
    return g


@pytest.fixture
def sample_message():
    """Standard test message."""
    return {
        "sender": "test_worker",
        "topic": "orchestrator",
        "type": "result",
        "content": "task completed successfully",
    }


# ---------------------------------------------------------------------------
# 1. Fingerprint tests
# ---------------------------------------------------------------------------

class TestFingerprint:
    """Test SpamGuard.fingerprint() hashing."""

    def test_fingerprint_deterministic(self, guard, sample_message):
        """Same message should always produce the same fingerprint."""
        fp1 = guard.fingerprint(sample_message)
        fp2 = guard.fingerprint(sample_message)
        assert fp1 == fp2
        # signed: gamma

    def test_fingerprint_differs_on_content(self, guard):
        """Different content should produce different fingerprints."""
        msg1 = {"sender": "alpha", "topic": "t", "type": "r", "content": "hello"}
        msg2 = {"sender": "alpha", "topic": "t", "type": "r", "content": "world"}
        assert guard.fingerprint(msg1) != guard.fingerprint(msg2)
        # signed: gamma

    def test_fingerprint_strips_timestamps(self, guard):
        """Timestamps in content should be normalized out."""
        msg1 = {"sender": "a", "topic": "t", "type": "r",
                "content": "done at 2026-03-20T15:30:00Z"}
        msg2 = {"sender": "a", "topic": "t", "type": "r",
                "content": "done at 2026-03-21T10:00:00Z"}
        assert guard.fingerprint(msg1) == guard.fingerprint(msg2)
        # signed: gamma

    def test_fingerprint_strips_uuids(self, guard):
        """UUIDs in content should be normalized out."""
        msg1 = {"sender": "a", "topic": "t", "type": "r",
                "content": "id=550e8400-e29b-41d4-a716-446655440000"}
        msg2 = {"sender": "a", "topic": "t", "type": "r",
                "content": "id=12345678-1234-1234-1234-123456789abc"}
        assert guard.fingerprint(msg1) == guard.fingerprint(msg2)
        # signed: gamma

    def test_fingerprint_is_16_char_hex(self, guard, sample_message):
        """Fingerprint should be a 16-char hex string."""
        fp = guard.fingerprint(sample_message)
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)
        # signed: gamma


# ---------------------------------------------------------------------------
# 2. Dedup tests
# ---------------------------------------------------------------------------

class TestDedup:
    """Test is_duplicate() dedup checking."""

    def test_first_message_not_duplicate(self, guard):
        """A fingerprint never seen before is not a duplicate."""
        assert guard.is_duplicate("abcdef0123456789") is False
        # signed: gamma

    def test_recorded_fingerprint_is_duplicate(self, guard):
        """After recording, the same fingerprint IS a duplicate within window."""
        fp = "test_fingerprint1"
        guard._record_fingerprint(fp)
        assert guard.is_duplicate(fp, window_seconds=60) is True
        # signed: gamma

    def test_expired_fingerprint_not_duplicate(self, guard):
        """A fingerprint older than the window is NOT a duplicate."""
        fp = "old_fingerprint"
        guard._state.setdefault("fingerprints", {})[fp] = time.time() - 1000
        assert guard.is_duplicate(fp, window_seconds=60) is False
        # signed: gamma


# ---------------------------------------------------------------------------
# 3. Rate limiting tests
# ---------------------------------------------------------------------------

class TestRateLimiting:
    """Test is_rate_limited() per-sender rate limiting."""

    def test_first_message_not_rate_limited(self, guard):
        """First message from a sender is never rate-limited."""
        result = guard.is_rate_limited("new_sender")
        assert result is None  # None = not rate-limited
        # signed: gamma

    def test_exceeding_per_minute_limit(self, guard):
        """Exceeding per-minute limit should trigger rate limiting."""
        sender = "spammy_worker"
        now = time.time()
        # Inject 5 timestamps in the last 30 seconds
        guard._state["sender_timestamps"] = {
            sender: [now - i for i in range(5)]
        }
        result = guard.is_rate_limited(sender, max_per_minute=5)
        assert result is not None
        assert "rate_limit_minute" in result
        # signed: gamma

    def test_exceeding_per_hour_limit(self, guard):
        """Exceeding per-hour limit should trigger rate limiting."""
        sender = "hourly_spammer"
        now = time.time()
        # Inject 30 timestamps spread across the last hour
        guard._state["sender_timestamps"] = {
            sender: [now - (i * 100) for i in range(30)]
        }
        result = guard.is_rate_limited(sender, max_per_minute=100, max_per_hour=30)
        assert result is not None
        assert "rate_limit_hour" in result
        # signed: gamma

    def test_monitor_has_higher_limit(self, guard):
        """Monitor sender should have override rate limits (10/min)."""
        sender = "monitor"
        now = time.time()
        # 6 messages in last minute — exceeds default (5) but not monitor (10)
        guard._state["sender_timestamps"] = {
            sender: [now - i for i in range(6)]
        }
        result = guard.is_rate_limited(sender)
        assert result is None  # Not rate-limited due to override
        # signed: gamma


# ---------------------------------------------------------------------------
# 4. Pattern-specific spam detection tests
# ---------------------------------------------------------------------------

class TestSpamPatterns:
    """Test _check_spam_patterns() for specific spam categories."""

    def test_daemon_health_spam(self, guard):
        """Rapid daemon_health messages should be blocked."""
        msg = {"sender": "monitor", "topic": "system", "type": "daemon_health",
               "content": "watchdog alive"}
        fp = guard.fingerprint(msg)
        # First should pass
        result1 = guard._check_spam_patterns(msg, fp)
        assert result1 is None
        # Second within 60s should be blocked
        result2 = guard._check_spam_patterns(msg, fp)
        assert result2 is not None
        assert "daemon_health" in result2
        # signed: gamma

    def test_dead_alert_dedup(self, guard):
        """Duplicate DEAD alerts for the same worker within 120s should be blocked."""
        msg = {"sender": "monitor", "topic": "orchestrator", "type": "alert",
               "content": "alpha is DEAD"}
        fp = guard.fingerprint(msg)
        result1 = guard._check_spam_patterns(msg, fp)
        assert result1 is None  # first passes
        result2 = guard._check_spam_patterns(msg, fp)
        assert result2 is not None
        assert "dead_alert" in result2
        # signed: gamma


# ---------------------------------------------------------------------------
# 5. guarded_publish() tests
# ---------------------------------------------------------------------------

class TestGuardedPublish:
    """Test the guarded_publish() convenience function."""

    def test_rejects_none_input(self):
        """guarded_publish(None) should return allowed=False."""
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish(None)
        assert result["allowed"] is False
        assert "invalid_message_type" in result["reason"]
        # signed: gamma

    def test_rejects_missing_sender(self):
        """Message without sender should be rejected."""
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish({"topic": "t", "type": "r", "content": "c"})
        assert result["allowed"] is False
        assert "missing required fields" in result["reason"]
        # signed: gamma

    def test_rejects_missing_content(self):
        """Message without content should be rejected."""
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish({"sender": "s", "topic": "t", "type": "r"})
        assert result["allowed"] is False
        assert "missing required fields" in result["reason"]
        # signed: gamma

    def test_rejects_non_dict(self):
        """String input should be rejected."""
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish("not a dict")
        assert result["allowed"] is False
        # signed: gamma


# ---------------------------------------------------------------------------
# 6. check_would_be_blocked() tests
# ---------------------------------------------------------------------------

class TestCheckWouldBeBlocked:
    """Test the read-only pre-flight check."""

    def test_fresh_message_not_blocked(self):
        """A fresh unique message should not be blocked."""
        from tools.skynet_spam_guard import check_would_be_blocked, _singleton_guard
        # Reset singleton to get clean state
        import tools.skynet_spam_guard as mod
        mod._singleton_guard = None

        # Use a unique message so dedup won't trigger
        msg = {
            "sender": "test_preflight",
            "topic": "test",
            "type": "check",
            "content": f"unique content {time.time()}",
        }
        with patch.object(mod, "STATE_FILE", Path("/nonexistent/path.json")):
            # Guard init may fail → conservative: not blocked
            result = check_would_be_blocked(msg)
        assert result["would_block"] is False
        # signed: gamma

    def test_returns_fingerprint(self):
        """check_would_be_blocked should always return a fingerprint."""
        from tools.skynet_spam_guard import check_would_be_blocked
        import tools.skynet_spam_guard as mod
        mod._singleton_guard = None

        msg = {"sender": "s", "topic": "t", "type": "r",
               "content": f"test {time.time()}"}
        result = check_would_be_blocked(msg)
        # Either has a fingerprint or guard errored (both are valid)
        assert "fingerprint" in result
        # signed: gamma


# ---------------------------------------------------------------------------
# 7. Fallback rate limiter tests
# ---------------------------------------------------------------------------

class TestFallbackRateLimiter:
    """Test _fallback_rate_ok() basic rate check."""

    def test_allows_first_call(self):
        """First call for a sender should be allowed."""
        from tools.skynet_spam_guard import _fallback_rate_ok
        # Use unique sender name to avoid cross-test pollution
        result = _fallback_rate_ok(f"fresh_sender_{time.time()}")
        assert result is True
        # signed: gamma

    def test_blocks_after_limit(self):
        """After max_per_minute calls, subsequent calls should be blocked."""
        from tools.skynet_spam_guard import _fallback_rate_ok, _fallback_timestamps
        sender = f"spammer_{time.time()}"
        # Pre-fill with max timestamps
        _fallback_timestamps[sender] = [time.time()] * 5
        result = _fallback_rate_ok(sender, max_per_minute=5)
        assert result is False
        # signed: gamma


# ---------------------------------------------------------------------------
# 8. Constants and configuration tests
# ---------------------------------------------------------------------------

class TestConstants:
    """Test module-level constants are sane."""

    def test_default_dedup_window(self):
        """DEFAULT_DEDUP_WINDOW should be 900 seconds (15 min)."""
        from tools.skynet_spam_guard import DEFAULT_DEDUP_WINDOW
        assert DEFAULT_DEDUP_WINDOW == 900
        # signed: gamma

    def test_default_rate_limits(self):
        """Default rate limits should be 5/min, 30/hour."""
        from tools.skynet_spam_guard import DEFAULT_MAX_PER_MINUTE, DEFAULT_MAX_PER_HOUR
        assert DEFAULT_MAX_PER_MINUTE == 5
        assert DEFAULT_MAX_PER_HOUR == 30
        # signed: gamma

    def test_spam_penalty_proportional(self):
        """SPAM_PENALTY should be proportional (not 10x the award)."""
        from tools.skynet_spam_guard import SPAM_PENALTY
        assert SPAM_PENALTY <= 0.1  # Must not be disproportionate
        # signed: gamma

    def test_pattern_windows_defined(self):
        """All expected pattern windows should be defined."""
        from tools.skynet_spam_guard import PATTERN_WINDOWS
        expected_keys = {"convene_gate_proposal", "convene_gate_vote",
                         "result_duplicate", "daemon_health",
                         "knowledge_learning", "dead_alert"}
        assert expected_keys.issubset(set(PATTERN_WINDOWS.keys()))
        # signed: gamma
