"""Tests for tools/skynet_spam_guard.py — Anti-spam rate limiter for the Skynet bus.

Tests cover: fingerprint computation & normalization, dedup window enforcement,
per-sender rate limiting, sender rate overrides, category-specific pattern windows
(DEAD 120s, daemon_health 60s, knowledge 1800s, result 300s, convene gate),
priority-aware rate limiting, SpamGuard score penalty integration,
check_would_be_blocked pre-flight, and guarded_publish input validation.

# signed: delta
"""

import hashlib
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the module-level singleton guard before each test."""
    import tools.skynet_spam_guard as sg
    sg._singleton_guard = None
    yield
    sg._singleton_guard = None


@pytest.fixture
def guard(tmp_path):
    """Create a fresh SpamGuard with isolated state files."""
    import tools.skynet_spam_guard as sg
    original_state = sg.STATE_FILE
    original_log = sg.LOG_FILE
    sg.STATE_FILE = tmp_path / "spam_guard_state.json"
    sg.LOG_FILE = tmp_path / "spam_log.json"
    sg.DATA_DIR = tmp_path
    g = sg.SpamGuard()
    g.reset()
    yield g
    sg.STATE_FILE = original_state
    sg.LOG_FILE = original_log
    sg.DATA_DIR = ROOT / "data"


@pytest.fixture
def sg_module(tmp_path):
    """Return the spam_guard module with isolated state paths."""
    import tools.skynet_spam_guard as sg
    original_state = sg.STATE_FILE
    original_log = sg.LOG_FILE
    sg.STATE_FILE = tmp_path / "spam_guard_state.json"
    sg.LOG_FILE = tmp_path / "spam_log.json"
    sg.DATA_DIR = tmp_path
    yield sg
    sg.STATE_FILE = original_state
    sg.LOG_FILE = original_log
    sg.DATA_DIR = ROOT / "data"
    sg._singleton_guard = None


def _msg(sender="test_worker", topic="orchestrator", msg_type="result",
         content="task completed", metadata=None):
    """Helper to build a bus message dict."""
    m = {"sender": sender, "topic": topic, "type": msg_type, "content": content}
    if metadata:
        m["metadata"] = metadata
    return m


# ── Fingerprint Tests ───────────────────────────────────────────────────────

class TestFingerprint:
    """Tests for SpamGuard.fingerprint() computation and normalization."""

    def test_deterministic(self, guard):
        """Same message produces same fingerprint."""
        msg = _msg(content="hello world")
        fp1 = guard.fingerprint(msg)
        fp2 = guard.fingerprint(msg)
        assert fp1 == fp2
        # signed: delta

    def test_length_is_16(self, guard):
        """Fingerprint is 16 hex chars (SHA-256 truncated)."""
        fp = guard.fingerprint(_msg())
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)
        # signed: delta

    def test_different_content_different_fp(self, guard):
        """Different content produces different fingerprints."""
        fp1 = guard.fingerprint(_msg(content="hello"))
        fp2 = guard.fingerprint(_msg(content="goodbye"))
        assert fp1 != fp2
        # signed: delta

    def test_different_sender_different_fp(self, guard):
        """Different senders produce different fingerprints."""
        fp1 = guard.fingerprint(_msg(sender="alpha"))
        fp2 = guard.fingerprint(_msg(sender="beta"))
        assert fp1 != fp2
        # signed: delta

    def test_different_topic_different_fp(self, guard):
        """Different topics produce different fingerprints."""
        fp1 = guard.fingerprint(_msg(topic="orchestrator"))
        fp2 = guard.fingerprint(_msg(topic="workers"))
        assert fp1 != fp2
        # signed: delta

    def test_timestamp_normalization(self, guard):
        """Timestamps in content are stripped before fingerprinting."""
        fp1 = guard.fingerprint(_msg(content="event at 2026-03-11T18:00:00Z"))
        fp2 = guard.fingerprint(_msg(content="event at 2026-03-12T09:30:00Z"))
        assert fp1 == fp2
        # signed: delta

    def test_uuid_normalization(self, guard):
        """UUIDs in content are normalized."""
        fp1 = guard.fingerprint(_msg(content="id=a1b2c3d4-e5f6-7890-abcd-ef1234567890"))
        fp2 = guard.fingerprint(_msg(content="id=11111111-2222-3333-4444-555555555555"))
        assert fp1 == fp2
        # signed: delta

    def test_cycle_number_normalization(self, guard):
        """Cycle numbers are normalized."""
        fp1 = guard.fingerprint(_msg(content="cycle=100 status ok"))
        fp2 = guard.fingerprint(_msg(content="cycle=999 status ok"))
        assert fp1 == fp2
        # signed: delta

    def test_gate_id_preserves_worker(self, guard):
        """Gate IDs preserve worker suffix for differentiation."""
        fp1 = guard.fingerprint(_msg(content="gate_123_alpha proposal"))
        fp2 = guard.fingerprint(_msg(content="gate_456_alpha proposal"))
        assert fp1 == fp2  # same worker = same fingerprint
        # signed: delta

    def test_gate_id_different_workers(self, guard):
        """Gate IDs for different workers produce different fingerprints."""
        fp1 = guard.fingerprint(_msg(content="gate_123_alpha proposal"))
        fp2 = guard.fingerprint(_msg(content="gate_123_beta proposal"))
        assert fp1 != fp2
        # signed: delta

    def test_pid_normalization(self, guard):
        """PID numbers are normalized."""
        fp1 = guard.fingerprint(_msg(content="process pid=12345 started"))
        fp2 = guard.fingerprint(_msg(content="process pid=99999 started"))
        assert fp1 == fp2
        # signed: delta

    def test_port_normalization(self, guard):
        """Port numbers are normalized."""
        fp1 = guard.fingerprint(_msg(content="listening port=8420"))
        fp2 = guard.fingerprint(_msg(content="listening port=9999"))
        assert fp1 == fp2
        # signed: delta

    def test_hwnd_normalization(self, guard):
        """HWND numbers are normalized."""
        fp1 = guard.fingerprint(_msg(content="window hwnd=123456"))
        fp2 = guard.fingerprint(_msg(content="window hwnd=789012"))
        assert fp1 == fp2
        # signed: delta

    def test_case_insensitive(self, guard):
        """Sender/topic/type are lowercased before hashing."""
        fp1 = guard.fingerprint(_msg(sender="ALPHA", topic="ORCHESTRATOR"))
        fp2 = guard.fingerprint(_msg(sender="alpha", topic="orchestrator"))
        assert fp1 == fp2
        # signed: delta

    def test_whitespace_normalization(self, guard):
        """Extra whitespace in content is collapsed."""
        fp1 = guard.fingerprint(_msg(content="hello  world"))
        fp2 = guard.fingerprint(_msg(content="hello world"))
        assert fp1 == fp2
        # signed: delta


# ── Dedup Window Tests ──────────────────────────────────────────────────────

class TestDedupWindow:
    """Tests for is_duplicate() dedup window enforcement."""

    def test_not_duplicate_first_time(self, guard):
        """First occurrence is never a duplicate."""
        fp = guard.fingerprint(_msg())
        assert guard.is_duplicate(fp) is False
        # signed: delta

    def test_duplicate_within_window(self, guard):
        """Same fingerprint within window IS a duplicate."""
        fp = guard.fingerprint(_msg())
        guard._record_fingerprint(fp)
        assert guard.is_duplicate(fp, window_seconds=900) is True
        # signed: delta

    def test_not_duplicate_after_window(self, guard):
        """Same fingerprint OUTSIDE window is NOT a duplicate."""
        fp = guard.fingerprint(_msg())
        # Record with a timestamp that's 1000 seconds old
        guard._state.setdefault("fingerprints", {})[fp] = time.time() - 1000
        assert guard.is_duplicate(fp, window_seconds=900) is False
        # signed: delta

    def test_custom_window(self, guard):
        """Custom dedup window is respected."""
        fp = guard.fingerprint(_msg())
        guard._state.setdefault("fingerprints", {})[fp] = time.time() - 50
        assert guard.is_duplicate(fp, window_seconds=60) is True
        assert guard.is_duplicate(fp, window_seconds=30) is False
        # signed: delta


# ── Rate Limiting Tests ─────────────────────────────────────────────────────

class TestRateLimiting:
    """Tests for is_rate_limited() per-sender rate limiting."""

    def test_no_limit_initially(self, guard):
        """No rate limit on first message."""
        result = guard.is_rate_limited("new_sender")
        assert result is None
        # signed: delta

    def test_per_minute_limit(self, guard):
        """Rate limit triggers at 5 messages/minute."""
        now = time.time()
        guard._state["sender_timestamps"] = {
            "fast_sender": [now - i for i in range(5)]
        }
        result = guard.is_rate_limited("fast_sender")
        assert result is not None
        assert "rate_limit_minute" in result
        assert "limit=5" in result
        # signed: delta

    def test_per_hour_limit(self, guard):
        """Rate limit triggers at 30 messages/hour."""
        now = time.time()
        # 30 messages spread over the last hour
        guard._state["sender_timestamps"] = {
            "bulk_sender": [now - (i * 100) for i in range(30)]
        }
        result = guard.is_rate_limited("bulk_sender")
        assert result is not None
        assert "rate_limit_hour" in result
        assert "limit=30" in result
        # signed: delta

    def test_under_limit_returns_none(self, guard):
        """Under limit returns None (OK)."""
        now = time.time()
        guard._state["sender_timestamps"] = {
            "normal_sender": [now - 20, now - 40]  # 2 msgs in last minute
        }
        result = guard.is_rate_limited("normal_sender")
        assert result is None
        # signed: delta

    def test_old_timestamps_pruned(self, guard):
        """Timestamps older than 1 hour are pruned."""
        now = time.time()
        guard._state["sender_timestamps"] = {
            "old_sender": [now - 7200, now - 7100]  # 2 hours old
        }
        result = guard.is_rate_limited("old_sender")
        assert result is None  # Old messages don't count
        # signed: delta


# ── Sender Rate Override Tests ──────────────────────────────────────────────

class TestSenderRateOverrides:
    """Tests for per-sender rate limit overrides."""

    def test_monitor_has_higher_limit(self, guard):
        """Monitor sender gets 10/min instead of 5/min."""
        now = time.time()
        # 6 messages in last minute — would block default (5) but not monitor (10)
        guard._state["sender_timestamps"] = {
            "monitor": [now - i for i in range(6)]
        }
        result = guard.is_rate_limited("monitor")
        assert result is None  # Not rate-limited with 10/min override
        # signed: delta

    def test_monitor_blocked_at_override_limit(self, guard):
        """Monitor gets blocked at 10/min (its override limit)."""
        now = time.time()
        guard._state["sender_timestamps"] = {
            "monitor": [now - i for i in range(10)]
        }
        result = guard.is_rate_limited("monitor")
        assert result is not None
        assert "limit=10" in result
        # signed: delta

    def test_system_sender_override(self, guard):
        """System sender uses override limits."""
        now = time.time()
        guard._state["sender_timestamps"] = {
            "system": [now - i for i in range(6)]
        }
        result = guard.is_rate_limited("system")
        assert result is None  # system gets 10/min
        # signed: delta

    def test_regular_sender_uses_default(self, guard):
        """Regular sender (no override) uses default 5/min."""
        now = time.time()
        guard._state["sender_timestamps"] = {
            "alpha": [now - i for i in range(5)]
        }
        result = guard.is_rate_limited("alpha")
        assert result is not None
        assert "limit=5" in result
        # signed: delta


# ── Category-Specific Pattern Window Tests ──────────────────────────────────

class TestPatternWindows:
    """Tests for category-specific spam windows: DEAD 120s, daemon_health 60s,
    knowledge 1800s, result 300s, convene gate."""

    def test_dead_alert_blocked_within_120s(self, guard):
        """DEAD alert for same worker blocked within 120s."""
        msg = _msg(sender="monitor", topic="orchestrator", msg_type="alert",
                   content="alpha is DEAD")
        fp = guard.fingerprint(msg)
        # Simulate a recent DEAD alert for alpha
        dead_fp = hashlib.sha256("dead_alert|alpha".encode()).hexdigest()[:16]
        guard._state.setdefault("fingerprints", {})[dead_fp] = time.time()
        reason = guard._check_spam_patterns(msg, fp)
        assert reason is not None
        assert "spam_dead_alert_dupe" in reason
        assert "alpha" in reason
        # signed: delta

    def test_dead_alert_allowed_after_120s(self, guard):
        """DEAD alert for same worker allowed after 120s."""
        msg = _msg(sender="monitor", topic="orchestrator", msg_type="alert",
                   content="alpha is DEAD")
        fp = guard.fingerprint(msg)
        dead_fp = hashlib.sha256("dead_alert|alpha".encode()).hexdigest()[:16]
        guard._state.setdefault("fingerprints", {})[dead_fp] = time.time() - 130
        reason = guard._check_spam_patterns(msg, fp)
        assert reason is None
        # signed: delta

    def test_dead_alert_different_workers_not_blocked(self, guard):
        """DEAD alerts for different workers are independent."""
        msg_alpha = _msg(sender="monitor", topic="orchestrator", msg_type="alert",
                         content="alpha is DEAD")
        msg_beta = _msg(sender="monitor", topic="orchestrator", msg_type="alert",
                        content="beta is DEAD")
        fp_alpha = guard.fingerprint(msg_alpha)
        fp_beta = guard.fingerprint(msg_beta)
        # Record alpha DEAD
        dead_fp_alpha = hashlib.sha256("dead_alert|alpha".encode()).hexdigest()[:16]
        guard._state.setdefault("fingerprints", {})[dead_fp_alpha] = time.time()
        # Beta should not be blocked
        reason = guard._check_spam_patterns(msg_beta, fp_beta)
        assert reason is None
        # signed: delta

    def test_daemon_health_blocked_within_60s(self, guard):
        """daemon_health from same sender blocked within 60s."""
        msg = _msg(sender="learner", msg_type="daemon_health",
                   content='{"daemon": "skynet_learner"}')
        fp = guard.fingerprint(msg)
        daemon_fp = hashlib.sha256("daemon_health|learner".encode()).hexdigest()[:16]
        guard._state.setdefault("fingerprints", {})[daemon_fp] = time.time()
        reason = guard._check_spam_patterns(msg, fp)
        assert reason is not None
        assert "spam_daemon_health" in reason
        # signed: delta

    def test_daemon_health_allowed_after_60s(self, guard):
        """daemon_health allowed after 60s."""
        msg = _msg(sender="learner", msg_type="daemon_health",
                   content='{"daemon": "skynet_learner"}')
        fp = guard.fingerprint(msg)
        daemon_fp = hashlib.sha256("daemon_health|learner".encode()).hexdigest()[:16]
        guard._state.setdefault("fingerprints", {})[daemon_fp] = time.time() - 65
        reason = guard._check_spam_patterns(msg, fp)
        assert reason is None
        # signed: delta

    def test_knowledge_learning_blocked_within_1800s(self, guard):
        """Knowledge/learning duplicate blocked within 1800s."""
        msg = _msg(sender="alpha", topic="knowledge", msg_type="learning",
                   content="discovered pattern X")
        fp = guard.fingerprint(msg)
        guard._record_fingerprint(fp)
        reason = guard._check_spam_patterns(msg, fp)
        assert reason is not None
        assert "spam_knowledge_dupe" in reason
        # signed: delta

    def test_knowledge_learning_allowed_after_1800s(self, guard):
        """Knowledge/learning allowed after 1800s."""
        msg = _msg(sender="alpha", topic="knowledge", msg_type="learning",
                   content="discovered pattern X")
        fp = guard.fingerprint(msg)
        guard._state.setdefault("fingerprints", {})[fp] = time.time() - 1900
        reason = guard._check_spam_patterns(msg, fp)
        assert reason is None
        # signed: delta

    def test_result_duplicate_blocked_within_300s(self, guard):
        """Identical result from same sender blocked within 300s."""
        msg = _msg(sender="alpha", msg_type="result", content="done task X")
        fp = guard.fingerprint(msg)
        guard._record_fingerprint(fp)
        reason = guard._check_spam_patterns(msg, fp)
        assert reason is not None
        assert "spam_result_dupe" in reason
        # signed: delta

    def test_result_duplicate_allowed_after_300s(self, guard):
        """Result allowed after 300s."""
        msg = _msg(sender="alpha", msg_type="result", content="done task X")
        fp = guard.fingerprint(msg)
        guard._state.setdefault("fingerprints", {})[fp] = time.time() - 310
        reason = guard._check_spam_patterns(msg, fp)
        assert reason is None
        # signed: delta

    def test_convene_gate_proposal_blocked_within_window(self, guard):
        """Convene gate-proposal duplicate blocked within 120s."""
        msg = _msg(sender="alpha", topic="convene", msg_type="gate-proposal",
                   content="proposal for security fix")
        fp = guard.fingerprint(msg)
        guard._record_fingerprint(fp)
        reason = guard._check_spam_patterns(msg, fp)
        assert reason is not None
        assert "spam_convene_gate_proposal" in reason
        # signed: delta

    def test_convene_gate_vote_duplicate_blocked(self, guard):
        """Same voter voting on same gate_id blocked."""
        msg = _msg(sender="beta", topic="convene", msg_type="gate-vote",
                   content="approve gate_123_alpha")
        fp = guard.fingerprint(msg)
        vote_fp = hashlib.sha256("vote|beta|gate_123_alpha".encode()).hexdigest()[:16]
        guard._state.setdefault("fingerprints", {})[vote_fp] = time.time()
        reason = guard._check_spam_patterns(msg, fp)
        assert reason is not None
        assert "spam_convene_vote_dupe" in reason
        # signed: delta


# ── publish_guarded Integration Tests ───────────────────────────────────────

class TestPublishGuarded:
    """Tests for publish_guarded() flow including dedup and rate limit enforcement."""

    def test_first_publish_allowed(self, guard):
        """First unique message passes all checks."""
        with patch.object(guard, '_bus_post', return_value=True):
            result = guard.publish_guarded(_msg(content="unique content 12345"))
        assert result["allowed"] is True
        assert result["published"] is True
        # signed: delta

    def test_duplicate_blocked(self, guard):
        """Identical message within dedup window is blocked."""
        msg = _msg(msg_type="info", content="duplicate test content")
        with patch.object(guard, '_bus_post', return_value=True):
            r1 = guard.publish_guarded(msg)
        assert r1["allowed"] is True
        # Second attempt should be blocked
        with patch.object(guard, '_auto_penalize'):
            r2 = guard.publish_guarded(msg)
        assert r2["allowed"] is False
        assert "dedup" in r2.get("reason", "") or "spam" in r2.get("reason", "")
        # signed: delta

    def test_rate_limited_blocked(self, guard):
        """Exceeding rate limit blocks message."""
        now = time.time()
        guard._state["sender_timestamps"] = {
            "alpha": [now - i for i in range(5)]
        }
        msg = _msg(sender="alpha", content="new unique message xyz")
        with patch.object(guard, '_auto_penalize'):
            result = guard.publish_guarded(msg)
        assert result["allowed"] is False
        assert "rate_limit" in result.get("reason", "")
        # signed: delta

    def test_stats_incremented_on_allow(self, guard):
        """Stats total_allowed increments on successful publish."""
        with patch.object(guard, '_bus_post', return_value=True):
            guard.publish_guarded(_msg(content="stats test abc"))
        stats = guard.get_stats()
        assert stats["total_allowed"] >= 1
        # signed: delta

    def test_stats_incremented_on_block(self, guard):
        """Stats total_blocked increments on spam block."""
        msg = _msg(content="block stats test def")
        fp = guard.fingerprint(msg)
        guard._record_fingerprint(fp)
        with patch.object(guard, '_auto_penalize'):
            guard.publish_guarded(msg)
        stats = guard.get_stats()
        assert stats["total_blocked"] >= 1
        # signed: delta

    def test_fingerprint_not_recorded_on_failed_post(self, guard):
        """If bus POST fails, fingerprint is NOT recorded (allows retry)."""
        msg = _msg(content="post failure test ghi")
        fp = guard.fingerprint(msg)
        with patch.object(guard, '_bus_post', return_value=False):
            result = guard.publish_guarded(msg)
        assert result["allowed"] is True
        assert result["published"] is False
        # Fingerprint should NOT be recorded
        assert fp not in guard._state.get("fingerprints", {})
        # signed: delta


# ── Priority-Aware Rate Limiting Tests ──────────────────────────────────────

class TestPriorityRateLimiting:
    """Tests for metadata.priority affecting rate limits."""

    def test_critical_bypasses_rate_limit(self, guard):
        """Critical priority bypasses rate limiting."""
        now = time.time()
        guard._state["sender_timestamps"] = {
            "alpha": [now - i for i in range(5)]
        }
        msg = _msg(sender="alpha", content="critical message jkl",
                   metadata={"priority": "critical"})
        with patch.object(guard, '_bus_post', return_value=True):
            result = guard.publish_guarded(msg)
        assert result["allowed"] is True
        # signed: delta

    def test_low_priority_stricter_limit(self, guard):
        """Low priority gets stricter rate limits (2/min)."""
        now = time.time()
        # 2 messages in last minute — would be OK at default 5/min
        # but triggers at low priority 2/min
        guard._state["sender_timestamps"] = {
            "beta": [now - 10, now - 20]
        }
        msg = _msg(sender="beta", content="low priority msg mno",
                   metadata={"priority": "low"})
        with patch.object(guard, '_auto_penalize'):
            result = guard.publish_guarded(msg)
        assert result["allowed"] is False
        assert "rate_limit" in result.get("reason", "")
        # signed: delta

    def test_normal_priority_default_limits(self, guard):
        """Normal priority uses default limits."""
        now = time.time()
        guard._state["sender_timestamps"] = {
            "gamma": [now - 10, now - 20]  # 2 msgs — under 5/min limit
        }
        msg = _msg(sender="gamma", content="normal priority pqr")
        with patch.object(guard, '_bus_post', return_value=True):
            result = guard.publish_guarded(msg)
        assert result["allowed"] is True
        # signed: delta


# ── Score Penalty Integration Tests ─────────────────────────────────────────

class TestScorePenalty:
    """Tests for auto-penalize on spam detection."""

    def test_penalty_applied_on_block(self, guard):
        """Blocked message triggers score penalty."""
        msg = _msg(sender="alpha", content="penalty test stu")
        fp = guard.fingerprint(msg)
        guard._record_fingerprint(fp)
        with patch("tools.skynet_scoring.adjust_score") as mock_score:
            guard.publish_guarded(msg)
        mock_score.assert_called_once()
        args = mock_score.call_args
        assert args[0][0] == "alpha"  # sender
        assert args[0][1] < 0  # negative penalty
        # signed: delta

    def test_penalty_exempt_for_system_senders(self, guard):
        """System senders (monitor, learner, etc.) exempt from penalties."""
        msg = _msg(sender="monitor", content="system exempt test vwx")
        fp = guard.fingerprint(msg)
        guard._record_fingerprint(fp)
        with patch("tools.skynet_scoring.adjust_score") as mock_score:
            guard.publish_guarded(msg)
        mock_score.assert_not_called()
        # signed: delta

    def test_penalty_exempt_for_convene_gate_dupes(self, guard):
        """Convene gate proposal dupes are exempt from penalties."""
        msg = _msg(sender="alpha", topic="convene", msg_type="gate-proposal",
                   content="proposal for fix xyz")
        fp = guard.fingerprint(msg)
        guard._record_fingerprint(fp)
        with patch("tools.skynet_scoring.adjust_score") as mock_score:
            guard.publish_guarded(msg)
        mock_score.assert_not_called()
        # signed: delta

    def test_penalty_exempt_for_convene_vote_dupes(self, guard):
        """Convene vote dupes are exempt from penalties."""
        msg = _msg(sender="beta", topic="convene", msg_type="gate-vote",
                   content="approve gate_100_alpha")
        fp = guard.fingerprint(msg)
        # Record vote fingerprint
        vote_fp = hashlib.sha256("vote|beta|gate_100_alpha".encode()).hexdigest()[:16]
        guard._state.setdefault("fingerprints", {})[vote_fp] = time.time()
        with patch("tools.skynet_scoring.adjust_score") as mock_score:
            guard.publish_guarded(msg)
        mock_score.assert_not_called()
        # signed: delta


# ── check_would_be_blocked Tests ────────────────────────────────────────────

class TestCheckWouldBeBlocked:
    """Tests for check_would_be_blocked() pre-flight check."""

    def test_not_blocked_first_time(self, sg_module):
        """First message would not be blocked."""
        result = sg_module.check_would_be_blocked(
            _msg(content="pre-flight test unique abc123")
        )
        assert result["would_block"] is False
        assert result["reason"] == ""
        assert "fingerprint" in result
        # signed: delta

    def test_blocked_after_publish(self, sg_module):
        """After publishing, same message would be blocked."""
        msg = _msg(msg_type="info", content="pre-flight after publish def456")
        with patch.object(sg_module.SpamGuard, '_bus_post', return_value=True):
            sg_module.guarded_publish(msg)
        result = sg_module.check_would_be_blocked(msg)
        assert result["would_block"] is True
        assert "dedup" in result["reason"] or "spam" in result["reason"]
        # signed: delta

    def test_no_side_effects(self, sg_module):
        """Pre-flight check does NOT record fingerprints or timestamps."""
        msg = _msg(content="no side effects test ghi789")
        result = sg_module.check_would_be_blocked(msg)
        assert result["would_block"] is False
        # Message should NOT be recorded — a real publish should still work
        with patch.object(sg_module.SpamGuard, '_bus_post', return_value=True):
            pub_result = sg_module.guarded_publish(msg)
        assert pub_result["allowed"] is True
        # signed: delta

    def test_returns_checks_dict(self, sg_module):
        """Result includes individual check results."""
        result = sg_module.check_would_be_blocked(
            _msg(content="checks dict test jkl012")
        )
        assert "checks" in result
        checks = result["checks"]
        assert "pattern" in checks
        assert "dedup" in checks
        assert "rate_limit" in checks
        # signed: delta


# ── guarded_publish Input Validation Tests ──────────────────────────────────

class TestGuardedPublishValidation:
    """Tests for guarded_publish() input validation."""

    def test_rejects_none(self, sg_module):
        """None input is rejected."""
        result = sg_module.guarded_publish(None)
        assert result["allowed"] is False
        assert "invalid_message_type" in result.get("reason", "")
        # signed: delta

    def test_rejects_string(self, sg_module):
        """String input is rejected."""
        result = sg_module.guarded_publish("not a dict")
        assert result["allowed"] is False
        assert "invalid_message_type" in result.get("reason", "")
        # signed: delta

    def test_rejects_missing_sender(self, sg_module):
        """Missing sender field is rejected."""
        result = sg_module.guarded_publish({"content": "hello", "topic": "test"})
        assert result["allowed"] is False
        assert "missing required fields" in result.get("reason", "")
        # signed: delta

    def test_rejects_missing_content(self, sg_module):
        """Missing content field is rejected."""
        result = sg_module.guarded_publish({"sender": "alpha", "topic": "test"})
        assert result["allowed"] is False
        assert "missing required fields" in result.get("reason", "")
        # signed: delta

    def test_rejects_empty_sender(self, sg_module):
        """Empty sender string is rejected."""
        result = sg_module.guarded_publish({"sender": "", "content": "hello"})
        assert result["allowed"] is False
        # signed: delta


# ── Reset and Stats Tests ───────────────────────────────────────────────────

class TestResetAndStats:
    """Tests for reset() and get_stats()."""

    def test_reset_clears_state(self, guard):
        """Reset clears all fingerprints and counters."""
        guard._record_fingerprint("test_fp")
        guard._record_sender_timestamp("alpha")
        guard.reset()
        stats = guard.get_stats()
        assert stats["active_fingerprints"] == 0
        assert stats["senders_tracked"] == 0
        assert stats["total_blocked"] == 0
        assert stats["total_allowed"] == 0
        # signed: delta

    def test_stats_structure(self, guard):
        """Stats returns expected fields."""
        stats = guard.get_stats()
        assert "total_blocked" in stats
        assert "total_allowed" in stats
        assert "blocked_by_pattern" in stats
        assert "blocked_by_sender" in stats
        assert "active_fingerprints" in stats
        assert "senders_tracked" in stats
        # signed: delta


# ── State Persistence Tests ─────────────────────────────────────────────────

class TestStatePersistence:
    """Tests for state file loading, saving, and pruning."""

    def test_save_and_load_roundtrip(self, tmp_path):
        """State survives save/load cycle."""
        import tools.skynet_spam_guard as sg
        orig_state, orig_log = sg.STATE_FILE, sg.LOG_FILE
        sg.STATE_FILE = tmp_path / "state.json"
        sg.LOG_FILE = tmp_path / "log.json"
        sg.DATA_DIR = tmp_path
        try:
            g = sg.SpamGuard()
            g.reset()
            g._record_fingerprint("abc123")
            g._record_sender_timestamp("alpha")
            g._save_state()
            g2 = sg.SpamGuard()  # loads from disk
            assert "abc123" in g2._state.get("fingerprints", {})
            assert "alpha" in g2._state.get("sender_timestamps", {})
        finally:
            sg.STATE_FILE, sg.LOG_FILE = orig_state, orig_log
            sg.DATA_DIR = ROOT / "data"
        # signed: gamma

    def test_old_fingerprints_pruned_on_load(self, tmp_path):
        """Fingerprints older than 2 hours are pruned on load."""
        import tools.skynet_spam_guard as sg
        orig_state, orig_log = sg.STATE_FILE, sg.LOG_FILE
        sg.STATE_FILE = tmp_path / "state.json"
        sg.LOG_FILE = tmp_path / "log.json"
        sg.DATA_DIR = tmp_path
        try:
            state = {
                "fingerprints": {
                    "recent": time.time() - 100,
                    "stale": time.time() - 8000,
                },
                "sender_timestamps": {},
                "stats": {"total_blocked": 0, "total_allowed": 0,
                          "blocked_by_pattern": {}, "blocked_by_sender": {}},
                "version": 1,
            }
            with open(sg.STATE_FILE, "w") as f:
                json.dump(state, f)
            g = sg.SpamGuard()
            assert "recent" in g._state["fingerprints"]
            assert "stale" not in g._state["fingerprints"]
        finally:
            sg.STATE_FILE, sg.LOG_FILE = orig_state, orig_log
            sg.DATA_DIR = ROOT / "data"
        # signed: gamma

    def test_old_sender_timestamps_pruned_on_load(self, tmp_path):
        """Sender timestamps older than 1 hour are pruned on load."""
        import tools.skynet_spam_guard as sg
        orig_state, orig_log = sg.STATE_FILE, sg.LOG_FILE
        sg.STATE_FILE = tmp_path / "state.json"
        sg.LOG_FILE = tmp_path / "log.json"
        sg.DATA_DIR = tmp_path
        try:
            now = time.time()
            state = {
                "fingerprints": {},
                "sender_timestamps": {
                    "alpha": [now - 100, now - 5000],
                    "dead_sender": [now - 5000],
                },
                "stats": {"total_blocked": 0, "total_allowed": 0,
                          "blocked_by_pattern": {}, "blocked_by_sender": {}},
                "version": 1,
            }
            with open(sg.STATE_FILE, "w") as f:
                json.dump(state, f)
            g = sg.SpamGuard()
            assert len(g._state["sender_timestamps"]["alpha"]) == 1
            assert "dead_sender" not in g._state["sender_timestamps"]
        finally:
            sg.STATE_FILE, sg.LOG_FILE = orig_state, orig_log
            sg.DATA_DIR = ROOT / "data"
        # signed: gamma

    def test_corrupted_state_file_handled(self, tmp_path):
        """Corrupted state file returns fresh state without crashing."""
        import tools.skynet_spam_guard as sg
        orig_state, orig_log = sg.STATE_FILE, sg.LOG_FILE
        sg.STATE_FILE = tmp_path / "state.json"
        sg.LOG_FILE = tmp_path / "log.json"
        sg.DATA_DIR = tmp_path
        try:
            with open(sg.STATE_FILE, "w") as f:
                f.write("{{{invalid json")
            g = sg.SpamGuard()
            assert g._state["version"] == 1
            assert g._state["fingerprints"] == {}
        finally:
            sg.STATE_FILE, sg.LOG_FILE = orig_state, orig_log
            sg.DATA_DIR = ROOT / "data"
        # signed: gamma


# ── Spam Log Tests ──────────────────────────────────────────────────────────

class TestSpamLog:
    """Tests for spam log file writing and rotation."""

    def test_spam_log_written_on_block(self, guard, tmp_path):
        """Blocked message is logged to spam_log.json."""
        import tools.skynet_spam_guard as sg
        sg.LOG_FILE = tmp_path / "spam_log.json"
        msg = _msg(sender="alpha", content="logged spam test 999")
        fp = guard.fingerprint(msg)
        guard._record_fingerprint(fp)
        with patch.object(guard, '_auto_penalize'):
            guard.publish_guarded(msg)
        assert sg.LOG_FILE.exists()
        with open(sg.LOG_FILE) as f:
            log = json.load(f)
        assert len(log["entries"]) >= 1
        entry = log["entries"][-1]
        assert entry["sender"] == "alpha"
        assert "fingerprint" in entry
        assert "reason" in entry
        # signed: gamma

    def test_spam_log_capped_at_500(self, guard, tmp_path):
        """Spam log keeps only the last 500 entries."""
        import tools.skynet_spam_guard as sg
        sg.LOG_FILE = tmp_path / "spam_log.json"
        log = {"entries": [{"sender": f"s{i}", "fingerprint": "x",
                            "reason": "test", "timestamp": "t",
                            "topic": "", "type": "", "content_preview": "",
                            "penalty": 0.02}
                           for i in range(499)], "version": 1}
        with open(sg.LOG_FILE, "w") as f:
            json.dump(log, f)
        for i in range(5):
            msg = _msg(sender="alpha", content=f"spam cap test {i}")
            fp = guard.fingerprint(msg)
            guard._record_fingerprint(fp)
            with patch.object(guard, '_auto_penalize'):
                guard._handle_spam("alpha", fp, "test", msg)
        with open(sg.LOG_FILE) as f:
            log = json.load(f)
        assert len(log["entries"]) <= 500
        # signed: gamma


# ── Edge Case Tests ─────────────────────────────────────────────────────────

class TestEdgeCases:
    """Edge cases for robustness."""

    def test_empty_content_fingerprint(self, guard):
        """Empty content still produces a valid fingerprint."""
        fp = guard.fingerprint(_msg(content=""))
        assert len(fp) == 16
        # signed: gamma

    def test_very_long_content(self, guard):
        """Very long content does not crash fingerprinting."""
        fp = guard.fingerprint(_msg(content="x" * 100000))
        assert len(fp) == 16
        # signed: gamma

    def test_special_chars_in_content(self, guard):
        """Special characters handled in fingerprinting."""
        fp = guard.fingerprint(_msg(content="alert! <script>hack</script> \x00"))
        assert len(fp) == 16
        # signed: gamma

    def test_missing_fields_in_message(self, guard):
        """Message with missing optional fields still fingerprints."""
        fp = guard.fingerprint({"sender": "alpha"})
        assert len(fp) == 16
        # signed: gamma

    def test_line_number_normalization(self, guard):
        """Line numbers are normalized."""
        fp1 = guard.fingerprint(_msg(content="error at line=42"))
        fp2 = guard.fingerprint(_msg(content="error at line=99"))
        assert fp1 == fp2
        # signed: gamma

    def test_remaining_normalization(self, guard):
        """Remaining hours are normalized."""
        fp1 = guard.fingerprint(_msg(content="remaining=5h until done"))
        fp2 = guard.fingerprint(_msg(content="remaining=12h until done"))
        assert fp1 == fp2
        # signed: gamma

    def test_latency_normalization(self, guard):
        """Latency values are normalized."""
        fp1 = guard.fingerprint(_msg(content="response latency=5.2ms"))
        fp2 = guard.fingerprint(_msg(content="response latency=100.0ms"))
        assert fp1 == fp2
        # signed: gamma

    def test_guarded_publish_fallback_on_guard_error(self, sg_module):
        """guarded_publish falls back to direct post on guard failure."""
        sg_module._singleton_guard = None
        with patch.object(sg_module.SpamGuard, '__init__',
                          side_effect=RuntimeError("init failed")):
            with patch.object(sg_module.SpamGuard, '_bus_post',
                              return_value=True) as mock_post:
                result = sg_module.guarded_publish(
                    _msg(content="fallback test xyz123"))
        assert result.get("fallback") is True
        # signed: gamma

    def test_bus_post_failure_no_fingerprint_recorded(self, guard):
        """When _bus_post fails, fingerprint is NOT recorded."""
        msg = _msg(content="bus fail test unique789")
        fp = guard.fingerprint(msg)
        with patch.object(guard, '_bus_post', return_value=False):
            guard.publish_guarded(msg)
        assert not guard.is_duplicate(fp, 900)
        # signed: gamma

    def test_concurrent_rate_limit_boundary(self, guard):
        """Rate limit at exactly the boundary."""
        now = time.time()
        guard._state["sender_timestamps"] = {
            "boundary": [now - 10, now - 20, now - 30, now - 40]
        }
        assert guard.is_rate_limited("boundary") is None
        guard._state["sender_timestamps"]["boundary"].append(now - 5)
        assert guard.is_rate_limited("boundary") is not None
        # signed: gamma
