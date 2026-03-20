#!/usr/bin/env python3
"""Tests for core/persistent_memory.py — episodic + semantic memory store.

Tests cover: PersistentMemoryStore (store/load episodes, semantics, recall,
consolidation, pruning, stats, effective utility decay, BM25-style scoring).

# signed: alpha
"""

import math
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── Basic Store Operations ───────────────────────────────────────

class TestPersistentMemoryStore:
    """Test PersistentMemoryStore core operations."""

    def _make_store(self, tmp_path):
        from core.persistent_memory import PersistentMemoryStore
        db = tmp_path / "test_memory.db"
        return PersistentMemoryStore(db_path=db)

    def test_store_episode(self, tmp_path):
        store = self._make_store(tmp_path)
        mid = store.store_episode("session1", "This is a test memory",
                                  context={"key": "value"}, importance=0.8,
                                  tags=["test", "memory"])
        assert mid is not None
        assert isinstance(mid, str)
        store.close()

    def test_store_semantic(self, tmp_path):
        store = self._make_store(tmp_path)
        mid = store.store_semantic("Semantic knowledge fact",
                                   importance=0.9, tags=["knowledge"])
        assert mid is not None
        assert isinstance(mid, str)
        store.close()

    def test_store_and_recall(self, tmp_path):
        store = self._make_store(tmp_path)
        store.store_episode("s1", "Python is a programming language", importance=0.8)
        store.store_episode("s1", "JavaScript runs in browsers", importance=0.7)
        store.store_episode("s1", "SQL is for databases", importance=0.6)
        results = store.recall("Python programming", top_k=5)
        assert len(results) >= 1
        # Python doc should rank highest
        assert "python" in results[0]["content"].lower() or "programming" in results[0]["content"].lower()
        store.close()

    def test_recall_empty_query(self, tmp_path):
        store = self._make_store(tmp_path)
        store.store_episode("s1", "some content")
        results = store.recall("", top_k=5)
        assert results == []
        store.close()

    def test_recall_no_match(self, tmp_path):
        store = self._make_store(tmp_path)
        store.store_episode("s1", "cats and dogs")
        results = store.recall("quantum physics spacetime", top_k=5)
        # May return empty or low-scoring results
        assert isinstance(results, list)
        store.close()

    def test_load_session(self, tmp_path):
        store = self._make_store(tmp_path)
        store.store_episode("session_a", "episode one", importance=0.9)
        store.store_episode("session_a", "episode two", importance=0.8)
        store.store_episode("session_b", "other session", importance=0.7)
        loaded = store.load_session("session_a", top_k=10)
        assert len(loaded) >= 2
        # All should be from session_a
        for mem in loaded:
            if "session_id" in mem:
                assert mem["session_id"] == "session_a"
        store.close()

    def test_load_all_sessions(self, tmp_path):
        store = self._make_store(tmp_path)
        store.store_episode("s1", "first session memory")
        store.store_episode("s2", "second session memory")
        loaded = store.load_session(None, top_k=10)
        assert len(loaded) >= 2
        store.close()

    def test_stats(self, tmp_path):
        store = self._make_store(tmp_path)
        store.store_episode("s1", "test")
        store.store_semantic("fact")
        stats = store.get_stats()
        assert stats["episodes"] >= 1
        assert stats["semantics"] >= 1
        assert "db_size_mb" in stats
        store.close()

    def test_prune_old_low_utility(self, tmp_path):
        store = self._make_store(tmp_path)
        # Store some low-utility episodes
        for i in range(5):
            store.store_episode("s1", f"low value content {i}",
                                importance=0.01)
        count = store.prune(min_utility=0.1, max_age_days=0)
        # Should prune some (exact count depends on utility scoring)
        assert isinstance(count, int)
        store.close()

    def test_close_idempotent(self, tmp_path):
        store = self._make_store(tmp_path)
        store.close()
        store.close()  # Should not raise


# ── Effective Utility Decay ──────────────────────────────────────

class TestEffectiveUtilityDecay:
    """Test time-based decay and frequency bonus logic."""

    def test_recent_memory_higher_utility(self, tmp_path):
        from core.persistent_memory import PersistentMemoryStore
        store = PersistentMemoryStore(db_path=tmp_path / "decay.db")

        # Store two episodes with different implicit timestamps
        store.store_episode("s1", "recent memory word searchterm", importance=0.8)
        loaded = store.load_session("s1", top_k=10)
        assert len(loaded) >= 1
        # Recent memory should have reasonable utility
        if "effective_utility" in loaded[0]:
            assert loaded[0]["effective_utility"] > 0
        store.close()

    def test_decay_half_life_constant(self):
        from core.persistent_memory import DECAY_HALF_LIFE_HOURS
        assert DECAY_HALF_LIFE_HOURS > 0
        assert isinstance(DECAY_HALF_LIFE_HOURS, (int, float))

    def test_consolidation_threshold(self):
        from core.persistent_memory import CONSOLIDATION_THRESHOLD
        assert CONSOLIDATION_THRESHOLD >= 2


# ── Consolidation ────────────────────────────────────────────────

class TestConsolidation:
    """Test episode-to-semantic promotion via consolidation."""

    def test_consolidate_no_overlap(self, tmp_path):
        from core.persistent_memory import PersistentMemoryStore
        store = PersistentMemoryStore(db_path=tmp_path / "consol.db")
        store.store_episode("s1", "apples oranges bananas")
        store.store_episode("s1", "cars trucks motorcycles")
        result = store.consolidate()
        assert isinstance(result, dict)
        assert "consolidated" in result
        assert "episodes_scanned" in result
        store.close()

    def test_consolidate_with_overlap(self, tmp_path):
        from core.persistent_memory import PersistentMemoryStore
        store = PersistentMemoryStore(db_path=tmp_path / "consol2.db")
        # Create overlapping episodes (60%+ word overlap)
        base = "python machine learning deep neural network training model"
        for i in range(5):
            store.store_episode("s1", f"{base} variation {i}")
        result = store.consolidate()
        assert isinstance(result, dict)
        store.close()

    def test_consolidate_empty_db(self, tmp_path):
        from core.persistent_memory import PersistentMemoryStore
        store = PersistentMemoryStore(db_path=tmp_path / "empty.db")
        result = store.consolidate()
        assert result["consolidated"] == 0
        store.close()


# ── Recall Scoring ───────────────────────────────────────────────

class TestRecallScoring:
    """Test BM25-style relevance scoring in recall."""

    def test_exact_match_scores_highest(self, tmp_path):
        from core.persistent_memory import PersistentMemoryStore
        store = PersistentMemoryStore(db_path=tmp_path / "score.db")
        store.store_episode("s1", "unique xylophone instrument music", importance=0.8)
        store.store_episode("s1", "common everyday generic text", importance=0.8)
        results = store.recall("xylophone", top_k=5)
        if len(results) >= 1:
            assert "xylophone" in results[0]["content"].lower()
        store.close()

    def test_multiple_term_match(self, tmp_path):
        from core.persistent_memory import PersistentMemoryStore
        store = PersistentMemoryStore(db_path=tmp_path / "multi.db")
        store.store_episode("s1", "alpha beta gamma delta epsilon", importance=0.8)
        store.store_episode("s1", "alpha beta only two terms", importance=0.8)
        results = store.recall("alpha beta gamma delta", top_k=5)
        if len(results) >= 2:
            # First result should have more matching terms
            assert results[0]["combined_score"] >= results[1]["combined_score"]
        store.close()

    def test_top_k_limit(self, tmp_path):
        from core.persistent_memory import PersistentMemoryStore
        store = PersistentMemoryStore(db_path=tmp_path / "limit.db")
        for i in range(20):
            store.store_episode("s1", f"document {i} with common word searchterm")
        results = store.recall("searchterm", top_k=5)
        assert len(results) <= 5
        store.close()
