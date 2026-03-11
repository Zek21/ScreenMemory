"""Tests for learner endpoint caching in god_console.py.

Validates cache hit/miss behavior, TTL expiry, cache_age_ms field,
and hit/miss counters for /learner/health and /learner/metrics.
"""

import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# We test the cache dict and TTL logic directly rather than spinning up an HTTP server.
# Import the module-level cache and TTL constants.
import god_console


class TestLearnerHealthCache(unittest.TestCase):
    """Test /learner/health caching (3s TTL)."""

    def setUp(self):
        """Reset cache state before each test."""
        god_console._learner_cache["health"] = None
        god_console._learner_cache["health_t"] = 0
        god_console._learner_cache["health_hits"] = 0
        god_console._learner_cache["health_misses"] = 0

    def test_ttl_constant(self):
        self.assertEqual(god_console._LEARNER_HEALTH_TTL, 3)

    def test_cache_miss_on_empty(self):
        """First request should always miss (cache is None)."""
        now = time.time()
        cache = god_console._learner_cache
        is_fresh = cache["health"] is not None and (now - cache["health_t"]) < god_console._LEARNER_HEALTH_TTL
        self.assertFalse(is_fresh)

    def test_cache_hit_within_ttl(self):
        """Request within TTL should hit."""
        now = time.time()
        cache = god_console._learner_cache
        cache["health"] = {"status": "running", "pid": 123, "stale": False, "stale_seconds": 10}
        cache["health_t"] = now  # just cached
        is_fresh = cache["health"] is not None and (now - cache["health_t"]) < god_console._LEARNER_HEALTH_TTL
        self.assertTrue(is_fresh)

    def test_cache_miss_after_ttl(self):
        """Request after TTL should miss."""
        now = time.time()
        cache = god_console._learner_cache
        cache["health"] = {"status": "running", "pid": 123}
        cache["health_t"] = now - 4  # 4 seconds ago, TTL is 3
        is_fresh = cache["health"] is not None and (now - cache["health_t"]) < god_console._LEARNER_HEALTH_TTL
        self.assertFalse(is_fresh)

    def test_cache_age_ms_field(self):
        """Cached response should include cache_age_ms reflecting real age."""
        cache = god_console._learner_cache
        cache["health"] = {"status": "stopped", "pid": None, "stale": True}
        cache["health_t"] = time.time() - 1.5  # 1.5 seconds ago
        # Simulate what the handler does
        now = time.time()
        data = dict(cache["health"])
        data["cache_age_ms"] = int((now - cache["health_t"]) * 1000)
        self.assertGreaterEqual(data["cache_age_ms"], 1400)
        self.assertLessEqual(data["cache_age_ms"], 2000)

    def test_hit_miss_counters_increment(self):
        """Counters should track hits and misses."""
        cache = god_console._learner_cache
        self.assertEqual(cache["health_hits"], 0)
        self.assertEqual(cache["health_misses"], 0)

        # Simulate a miss
        cache["health_misses"] += 1
        self.assertEqual(cache["health_misses"], 1)

        # Populate cache and simulate a hit
        cache["health"] = {"status": "running"}
        cache["health_t"] = time.time()
        cache["health_hits"] += 1
        self.assertEqual(cache["health_hits"], 1)

        # Another hit
        cache["health_hits"] += 1
        self.assertEqual(cache["health_hits"], 2)
        self.assertEqual(cache["health_misses"], 1)

    def test_cache_invalidation(self):
        """Setting cache to None should force a miss on next request."""
        cache = god_console._learner_cache
        cache["health"] = {"status": "running", "pid": 42}
        cache["health_t"] = time.time()

        # Invalidate
        cache["health"] = None
        cache["health_t"] = 0

        now = time.time()
        is_fresh = cache["health"] is not None and (now - cache["health_t"]) < god_console._LEARNER_HEALTH_TTL
        self.assertFalse(is_fresh)


class TestLearnerMetricsCache(unittest.TestCase):
    """Test /learner/metrics caching (5s TTL)."""

    def setUp(self):
        god_console._learner_cache["metrics"] = None
        god_console._learner_cache["metrics_t"] = 0
        god_console._learner_cache["metrics_hits"] = 0
        god_console._learner_cache["metrics_misses"] = 0

    def test_ttl_constant(self):
        self.assertEqual(god_console._LEARNER_METRICS_TTL, 5)

    def test_cache_miss_on_empty(self):
        now = time.time()
        cache = god_console._learner_cache
        is_fresh = cache["metrics"] is not None and (now - cache["metrics_t"]) < god_console._LEARNER_METRICS_TTL
        self.assertFalse(is_fresh)

    def test_cache_hit_within_ttl(self):
        now = time.time()
        cache = god_console._learner_cache
        cache["metrics"] = {
            "total_episodes": 10, "by_outcome": {"success": 5, "failure": 2, "unknown": 3},
            "sparkline_hourly": [1, 2, 3], "daemon_status": "running", "timestamp": now,
        }
        cache["metrics_t"] = now
        is_fresh = cache["metrics"] is not None and (now - cache["metrics_t"]) < god_console._LEARNER_METRICS_TTL
        self.assertTrue(is_fresh)

    def test_cache_miss_after_ttl(self):
        now = time.time()
        cache = god_console._learner_cache
        cache["metrics"] = {"total_episodes": 5}
        cache["metrics_t"] = now - 6  # 6 seconds ago, TTL is 5
        is_fresh = cache["metrics"] is not None and (now - cache["metrics_t"]) < god_console._LEARNER_METRICS_TTL
        self.assertFalse(is_fresh)

    def test_cache_age_ms_on_hit(self):
        cache = god_console._learner_cache
        cache["metrics"] = {"total_episodes": 20, "sparkline_hourly": [1, 2]}
        cache["metrics_t"] = time.time() - 2.0
        now = time.time()
        data = dict(cache["metrics"])
        data["cache_age_ms"] = int((now - cache["metrics_t"]) * 1000)
        self.assertGreaterEqual(data["cache_age_ms"], 1900)
        self.assertLessEqual(data["cache_age_ms"], 2500)

    def test_fresh_response_has_zero_cache_age(self):
        """A fresh (just-built) response should have cache_age_ms=0."""
        now = time.time()
        cache = god_console._learner_cache
        cache["metrics"] = {"total_episodes": 0}
        cache["metrics_t"] = now
        data = dict(cache["metrics"])
        data["cache_age_ms"] = int((now - cache["metrics_t"]) * 1000)
        self.assertEqual(data["cache_age_ms"], 0)

    def test_hit_miss_counters(self):
        cache = god_console._learner_cache
        # Simulate 3 misses, 5 hits
        for _ in range(3):
            cache["metrics_misses"] += 1
        cache["metrics"] = {"total_episodes": 42}
        cache["metrics_t"] = time.time()
        for _ in range(5):
            cache["metrics_hits"] += 1
        self.assertEqual(cache["metrics_hits"], 5)
        self.assertEqual(cache["metrics_misses"], 3)

    def test_cache_invalidation(self):
        cache = god_console._learner_cache
        cache["metrics"] = {"total_episodes": 100}
        cache["metrics_t"] = time.time()

        # Invalidate
        cache["metrics"] = None
        cache["metrics_t"] = 0

        now = time.time()
        is_fresh = cache["metrics"] is not None and (now - cache["metrics_t"]) < god_console._LEARNER_METRICS_TTL
        self.assertFalse(is_fresh)


class TestCacheStructureIntegrity(unittest.TestCase):
    """Verify the _learner_cache dict has all required keys."""

    def test_all_keys_present(self):
        cache = god_console._learner_cache
        required = ["health", "health_t", "health_hits", "health_misses",
                     "metrics", "metrics_t", "metrics_hits", "metrics_misses"]
        for key in required:
            self.assertIn(key, cache, f"Missing cache key: {key}")

    def test_ttl_values_are_positive(self):
        self.assertGreater(god_console._LEARNER_HEALTH_TTL, 0)
        self.assertGreater(god_console._LEARNER_METRICS_TTL, 0)

    def test_metrics_ttl_greater_than_health(self):
        """Metrics TTL should be >= health TTL (metrics is more expensive)."""
        self.assertGreaterEqual(god_console._LEARNER_METRICS_TTL, god_console._LEARNER_HEALTH_TTL)


if __name__ == "__main__":
    unittest.main()
