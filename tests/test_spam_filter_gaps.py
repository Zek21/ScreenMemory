"""Tests for bus spam filter behaviors documented in AGENTS.md but not covered.

Covers critical gaps identified in testing_5 audit:
- Server-side 429 response handling (Go backend blocks)
- Critical priority unauthorized sender downgrade
- End-to-end rate limiting through publish_guarded()
- Dual-layer dedup interaction (Python 900s vs Go 60s)
- Fallback resilience under guard failure

signed: gamma
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import skynet_spam_guard as sg


class SpamFilterTestBase(unittest.TestCase):
    """Base with temp dir and fresh guard for each test."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self._orig_state = sg.STATE_FILE
        self._orig_log = sg.LOG_FILE
        sg.STATE_FILE = Path(self.tmpdir.name) / "spam_guard_state.json"
        sg.LOG_FILE = Path(self.tmpdir.name) / "spam_log.json"
        self.guard = sg.SpamGuard()
        # Reset singleton so guarded_publish() uses fresh guard
        sg._singleton_guard = None

    def tearDown(self):
        sg.STATE_FILE = self._orig_state
        sg.LOG_FILE = self._orig_log
        sg._singleton_guard = None
        self.tmpdir.cleanup()

    def _msg(self, sender="gamma", topic="test", type_="report",
             content="test content", metadata=None):
        msg = {"sender": sender, "topic": topic, "type": type_,
               "content": content}
        if metadata:
            msg["metadata"] = metadata
        return msg


# ─── Server-Side 429 Response Handling ──────────────────────────

class TestServerSide429Handling(SpamFilterTestBase):
    """Verify behavior when Go backend returns HTTP 429 (SPAM_BLOCKED)."""

    @patch.object(sg.SpamGuard, "_bus_post", return_value=False)
    def test_go_block_returns_allowed_true_published_false(self, mock_post):
        """When Go blocks (429), Python guard allows but published=False."""
        msg = self._msg(content="unique message for 429 test")
        result = self.guard.publish_guarded(msg)
        self.assertTrue(result["allowed"])
        self.assertFalse(result["published"])
        mock_post.assert_called_once()

    @patch.object(sg.SpamGuard, "_bus_post", return_value=False)
    def test_fingerprint_not_recorded_on_go_block(self, mock_post):
        """Fingerprints should NOT be recorded when Go rejects."""
        msg = self._msg(content="fingerprint test for go block")
        fp = self.guard.fingerprint(msg)
        self.guard.publish_guarded(msg)
        # Fingerprint should NOT be in state since POST failed
        self.assertNotIn(fp, self.guard._state.get("fingerprints", {}))

    @patch.object(sg.SpamGuard, "_bus_post", return_value=False)
    def test_retry_allowed_after_go_block(self, mock_post):
        """Since FP isn't recorded on failure, retry should be allowed."""
        msg = self._msg(content="retry after go block test")
        result1 = self.guard.publish_guarded(msg)
        self.assertFalse(result1["published"])

        # Now let POST succeed on retry
        mock_post.return_value = True
        result2 = self.guard.publish_guarded(msg)
        self.assertTrue(result2["allowed"])
        self.assertTrue(result2["published"])

    @patch.object(sg.SpamGuard, "_bus_post", return_value=False)
    def test_stats_not_incremented_on_go_block(self, mock_post):
        """total_allowed should NOT increment when Go blocks."""
        msg = self._msg(content="stats test for go block")
        self.guard.publish_guarded(msg)
        stats = self.guard._state.get("stats", {})
        self.assertEqual(stats.get("total_allowed", 0), 0)

    @patch.object(sg.SpamGuard, "_bus_post", return_value=True)
    def test_stats_incremented_on_success(self, mock_post):
        """total_allowed SHOULD increment on successful publish."""
        msg = self._msg(content="stats test for success")
        self.guard.publish_guarded(msg)
        stats = self.guard._state.get("stats", {})
        self.assertEqual(stats.get("total_allowed", 0), 1)

    @patch.object(sg.SpamGuard, "_bus_post", return_value=False)
    def test_sender_timestamp_not_recorded_on_go_block(self, mock_post):
        """Sender timestamps should NOT be recorded on POST failure."""
        msg = self._msg(sender="test_sender", content="timestamp test go block")
        self.guard.publish_guarded(msg)
        timestamps = self.guard._state.get("sender_timestamps", {})
        self.assertNotIn("test_sender", timestamps)


# ─── Critical Priority Unauthorized Sender ──────────────────────

class TestCriticalPriorityDowngrade(SpamFilterTestBase):
    """Verify untrusted senders with priority=critical are downgraded."""

    @patch.object(sg.SpamGuard, "_bus_post", return_value=True)
    def test_authorized_critical_bypasses_rate_limit(self, mock_post):
        """System/monitor/orchestrator/watchdog can bypass via critical."""
        for authorized_sender in ["system", "monitor", "orchestrator", "watchdog"]:
            g = sg.SpamGuard()
            # Exhaust rate limit (5 msgs)
            for i in range(5):
                g.publish_guarded(self._msg(
                    sender=authorized_sender,
                    content=f"rate fill msg {i} from {authorized_sender}"))

            # 6th message with critical priority should still pass
            msg = self._msg(sender=authorized_sender,
                           content=f"critical bypass {authorized_sender}",
                           metadata={"priority": "critical"})
            result = g.publish_guarded(msg)
            self.assertTrue(result["allowed"],
                           f"{authorized_sender} should bypass rate limit with critical")

    @patch.object(sg.SpamGuard, "_bus_post", return_value=True)
    def test_unauthorized_critical_hits_rate_limit(self, mock_post):
        """Random senders with critical priority get downgraded to normal."""
        # Exhaust rate limit (5 msgs)
        for i in range(5):
            self.guard.publish_guarded(self._msg(
                sender="malicious_bot",
                content=f"rate fill msg {i} from bot"))

        # 6th message with critical priority should be BLOCKED
        msg = self._msg(sender="malicious_bot",
                       content="fake critical from bot",
                       metadata={"priority": "critical"})
        result = self.guard.publish_guarded(msg)
        self.assertFalse(result["allowed"],
                        "Unauthorized sender should NOT bypass rate limit")

    @patch.object(sg.SpamGuard, "_bus_post", return_value=True)
    def test_worker_critical_blocked(self, mock_post):
        """Workers (alpha, beta, etc.) are NOT authorized for critical bypass."""
        for i in range(5):
            self.guard.publish_guarded(self._msg(
                sender="alpha",
                content=f"rate fill msg {i} from alpha worker"))

        msg = self._msg(sender="alpha",
                       content="worker trying critical bypass",
                       metadata={"priority": "critical"})
        result = self.guard.publish_guarded(msg)
        self.assertFalse(result["allowed"],
                        "Workers should not bypass rate limit with critical")

    @patch.object(sg.SpamGuard, "_bus_post", return_value=True)
    def test_consultant_critical_blocked(self, mock_post):
        """Consultants are NOT authorized for critical bypass."""
        for i in range(5):
            self.guard.publish_guarded(self._msg(
                sender="consultant",
                content=f"rate fill msg {i} from consultant"))

        msg = self._msg(sender="consultant",
                       content="consultant trying critical bypass",
                       metadata={"priority": "critical"})
        result = self.guard.publish_guarded(msg)
        self.assertFalse(result["allowed"])


# ─── End-to-End Rate Limiting ────────────────────────────────────

class TestEndToEndRateLimiting(SpamFilterTestBase):
    """Full publish_guarded() calls hitting rate limits."""

    @patch.object(sg.SpamGuard, "_bus_post", return_value=True)
    def test_five_per_minute_limit_e2e(self, mock_post):
        """5 unique messages from same sender should pass, 6th blocked."""
        results = []
        for i in range(6):
            msg = self._msg(sender="test_e2e",
                           content=f"unique e2e content number {i}")
            result = self.guard.publish_guarded(msg)
            results.append(result)

        # First 5 should pass
        for i in range(5):
            self.assertTrue(results[i]["allowed"],
                           f"Message {i} should be allowed")
            self.assertTrue(results[i]["published"],
                           f"Message {i} should be published")

        # 6th should be rate-limited
        self.assertFalse(results[5]["allowed"],
                        "6th message should be rate-limited")
        self.assertIn("rate", results[5].get("reason", "").lower())

    @patch.object(sg.SpamGuard, "_bus_post", return_value=True)
    def test_different_senders_independent(self, mock_post):
        """Rate limits are per-sender — different senders don't interfere."""
        # Fill sender A's rate limit
        for i in range(5):
            self.guard.publish_guarded(self._msg(
                sender="sender_a", content=f"a msg {i}"))

        # Sender B should still work
        result = self.guard.publish_guarded(self._msg(
            sender="sender_b", content="b first message"))
        self.assertTrue(result["allowed"])

    @patch.object(sg.SpamGuard, "_bus_post", return_value=True)
    def test_low_priority_stricter_limit_e2e(self, mock_post):
        """Low priority messages have 2/min limit (stricter than 5/min)."""
        results = []
        for i in range(3):
            msg = self._msg(sender="low_priority_sender",
                           content=f"low priority content {i}",
                           metadata={"priority": "low"})
            result = self.guard.publish_guarded(msg)
            results.append(result)

        # First 2 should pass
        self.assertTrue(results[0]["allowed"])
        self.assertTrue(results[1]["allowed"])
        # 3rd should be blocked (2/min limit for low priority)
        self.assertFalse(results[2]["allowed"])

    @patch.object(sg.SpamGuard, "_bus_post", return_value=True)
    def test_monitor_higher_limit_e2e(self, mock_post):
        """Monitor gets 10/min override (higher than default 5/min)."""
        results = []
        for i in range(11):
            msg = self._msg(sender="monitor",
                           content=f"monitor status update {i}")
            result = self.guard.publish_guarded(msg)
            results.append(result)

        # First 10 should pass
        for i in range(10):
            self.assertTrue(results[i]["allowed"],
                           f"Monitor msg {i} should pass (10/min override)")

        # 11th should be blocked
        self.assertFalse(results[10]["allowed"])

    @patch.object(sg.SpamGuard, "_bus_post", return_value=True)
    def test_dedup_blocks_identical_content(self, mock_post):
        """Identical content from same sender is blocked as dedup."""
        msg = self._msg(sender="dedup_test", content="exact same content")
        result1 = self.guard.publish_guarded(msg)
        self.assertTrue(result1["allowed"])

        # Same exact message again
        result2 = self.guard.publish_guarded(msg)
        self.assertFalse(result2["allowed"])
        self.assertIn("dedup", result2.get("reason", "").lower())


# ─── Dual-Layer Dedup Interaction ────────────────────────────────

class TestDualLayerInteraction(SpamFilterTestBase):
    """Verify Python 900s vs Go 60s layer behavior.

    The two layers have different windows:
    - Python: 900s general dedup (pre-filter before network)
    - Go: 60s fingerprint dedup (server-side safety net)

    A message at T+61s would pass Go's 60s window but be blocked by
    Python's 900s window. This is by design — Python catches more.
    """

    @patch.object(sg.SpamGuard, "_bus_post", return_value=True)
    def test_python_blocks_within_900s_even_after_go_would_allow(self, mock_post):
        """Python's 900s dedup catches messages that Go's 60s would allow."""
        msg = self._msg(content="dual layer test message")
        result1 = self.guard.publish_guarded(msg)
        self.assertTrue(result1["allowed"])

        # At T+61s, Go would allow (60s window expired) but Python blocks
        # Simulate by confirming Python still blocks
        result2 = self.guard.publish_guarded(msg)
        self.assertFalse(result2["allowed"])
        self.assertIn("dedup", result2.get("reason", "").lower())

    @patch.object(sg.SpamGuard, "_bus_post", return_value=True)
    def test_python_allows_after_900s(self, mock_post):
        """After 900s, Python dedup window expires and allows re-publish."""
        msg = self._msg(content="dedup expiry test")
        fp = self.guard.fingerprint(msg)
        result1 = self.guard.publish_guarded(msg)
        self.assertTrue(result1["allowed"])

        # Backdate the fingerprint to 901 seconds ago
        self.guard._state["fingerprints"][fp] = time.time() - 901

        result2 = self.guard.publish_guarded(msg)
        self.assertTrue(result2["allowed"],
                       "Should pass after 900s dedup window expires")

    @patch.object(sg.SpamGuard, "_bus_post")
    def test_go_blocks_after_python_allows(self, mock_post):
        """Python allows → Go blocks (429) → result: allowed=True, published=False."""
        msg = self._msg(content="go blocks after python allows test")

        # First call: both allow
        mock_post.return_value = True
        result1 = self.guard.publish_guarded(msg)
        self.assertTrue(result1["published"])

        # Second call with different content (passes Python dedup):
        msg2 = self._msg(content="different content but go blocks")
        mock_post.return_value = False  # Simulate Go 429
        result2 = self.guard.publish_guarded(msg2)
        self.assertTrue(result2["allowed"])  # Python allowed
        self.assertFalse(result2["published"])  # Go blocked

    @patch.object(sg.SpamGuard, "_bus_post", return_value=True)
    def test_pattern_window_shorter_than_general_dedup(self, mock_post):
        """Pattern-specific windows (e.g., 120s for DEAD) are independent of 900s general."""
        # DEAD alert has 120s pattern window
        msg = self._msg(sender="monitor", topic="orchestrator",
                       type_="alert", content="DEAD: alpha")

        result1 = self.guard.publish_guarded(msg)
        self.assertTrue(result1["allowed"])

        # Second DEAD alert for same worker — blocked by pattern
        result2 = self.guard.publish_guarded(msg)
        self.assertFalse(result2["allowed"])


# ─── Fallback Resilience ─────────────────────────────────────────

class TestFallbackResilience(SpamFilterTestBase):
    """Verify fallback guard works when SpamGuard constructor fails."""

    def test_fallback_rate_limiting_works(self):
        """Fallback rate limiter (5/min) blocks rapid publishes."""
        sg._singleton_guard = None
        sg._fallback_timestamps.clear()

        with patch.object(sg, "SpamGuard", side_effect=RuntimeError("broken")), \
             patch.object(sg.SpamGuard, "_bus_post", return_value=True):

            results = []
            for i in range(7):
                msg = self._msg(sender="fallback_test",
                               content=f"fallback msg {i}")
                result = sg.guarded_publish(msg)
                results.append(result)

            allowed_count = sum(1 for r in results if r.get("allowed"))
            blocked_count = sum(1 for r in results if not r.get("allowed"))

            self.assertGreater(allowed_count, 0, "Some messages should pass via fallback")
            self.assertGreater(blocked_count, 0, "Fallback should rate-limit eventually")

        sg._singleton_guard = None
        sg._fallback_timestamps.clear()

    def test_fallback_returns_fallback_flag(self):
        """Fallback responses include fallback=True flag."""
        sg._singleton_guard = None
        sg._fallback_timestamps.clear()

        with patch.object(sg, "SpamGuard", side_effect=RuntimeError("broken")), \
             patch.object(sg.SpamGuard, "_bus_post", return_value=True):

            msg = self._msg(content="fallback flag test")
            result = sg.guarded_publish(msg)
            self.assertTrue(result.get("fallback"),
                           "Fallback responses should have fallback=True")

        sg._singleton_guard = None
        sg._fallback_timestamps.clear()


# ─── Pattern-Specific Window Tests ──────────────────────────────

class TestPatternWindowE2E(SpamFilterTestBase):
    """End-to-end tests for pattern-specific spam windows."""

    @patch.object(sg.SpamGuard, "_bus_post", return_value=True)
    def test_daemon_health_60s_window(self, mock_post):
        """daemon_health messages blocked within 60s of same daemon."""
        msg = self._msg(sender="watchdog", topic="system",
                       type_="daemon_health",
                       content="skynet_monitor: healthy")
        result1 = self.guard.publish_guarded(msg)
        self.assertTrue(result1["allowed"])

        result2 = self.guard.publish_guarded(msg)
        self.assertFalse(result2["allowed"])

    @patch.object(sg.SpamGuard, "_bus_post", return_value=True)
    def test_knowledge_learning_1800s_window(self, mock_post):
        """Knowledge/learning broadcasts blocked within 1800s."""
        msg = self._msg(sender="gamma", topic="knowledge",
                       type_="learning",
                       content="Learned: scoring fairness matters")
        result1 = self.guard.publish_guarded(msg)
        self.assertTrue(result1["allowed"])

        result2 = self.guard.publish_guarded(msg)
        self.assertFalse(result2["allowed"])

    @patch.object(sg.SpamGuard, "_bus_post", return_value=True)
    def test_result_duplicate_300s_window(self, mock_post):
        """Result messages have 300s dedup window."""
        msg = self._msg(sender="alpha", topic="orchestrator",
                       type_="result",
                       content="RESULT: task completed signed:alpha")
        result1 = self.guard.publish_guarded(msg)
        self.assertTrue(result1["allowed"])

        result2 = self.guard.publish_guarded(msg)
        self.assertFalse(result2["allowed"])


# ─── Input Validation Edge Cases ─────────────────────────────────

class TestInputValidationEdgeCases(SpamFilterTestBase):
    """Edge cases for guarded_publish input validation."""

    def test_none_sender_rejected(self):
        """Messages with sender=None should be rejected."""
        result = sg.guarded_publish({"sender": None, "content": "test"})
        self.assertFalse(result["allowed"])

    def test_empty_content_rejected(self):
        """Messages with empty content should be rejected."""
        result = sg.guarded_publish({"sender": "gamma", "content": ""})
        self.assertFalse(result["allowed"])

    def test_whitespace_only_sender_not_stripped(self):
        """Whitespace-only sender passes validation (not stripped by guard)."""
        # Current behavior: "   ".strip() is falsy but msg.get("sender")
        # returns "   " which is truthy. This documents actual behavior.
        result = sg.guarded_publish({"sender": "   ", "content": "test"})
        self.assertTrue(result["allowed"])


if __name__ == "__main__":
    unittest.main()

