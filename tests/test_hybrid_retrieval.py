#!/usr/bin/env python3
"""Tests for core/hybrid_retrieval.py — BM25 + RRF fusion retrieval.

Tests cover: BM25Index (tokenize, add/remove, search, IDF rebuild),
HybridRetriever (index, search, RRF fusion, vector/memory fallback),
RetrievalResult/FusedResult dataclasses, stats property, error handling.

# signed: alpha
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── BM25Index Tests ──────────────────────────────────────────────

class TestBM25Index:
    """Test BM25Index text search engine."""

    def _make_index(self):
        from core.hybrid_retrieval import BM25Index
        return BM25Index()

    def test_empty_search_returns_empty(self):
        idx = self._make_index()
        results = idx.search("anything")
        assert results == []

    def test_empty_query_returns_empty(self):
        idx = self._make_index()
        idx.add_document("d1", "hello world")
        results = idx.search("")
        assert results == []

    def test_add_and_search_single_doc(self):
        idx = self._make_index()
        idx.add_document("d1", "the quick brown fox jumps over the lazy dog")
        results = idx.search("fox")
        assert len(results) >= 1
        assert results[0].id == "d1"
        assert results[0].score > 0

    def test_search_returns_sorted_by_score(self):
        idx = self._make_index()
        idx.add_document("d1", "python programming language")
        idx.add_document("d2", "python python python scripting")
        idx.add_document("d3", "java enterprise framework")
        results = idx.search("python")
        assert len(results) >= 2
        # d2 has more "python" occurrences, should score higher
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_search_ranks_are_1_indexed(self):
        idx = self._make_index()
        idx.add_document("d1", "alpha testing framework")
        idx.add_document("d2", "beta testing suite")
        results = idx.search("testing")
        if results:
            assert results[0].rank == 1

    def test_remove_document(self):
        idx = self._make_index()
        idx.add_document("d1", "unique word xylophone")
        idx.add_document("d2", "another document")
        idx.remove_document("d1")
        results = idx.search("xylophone")
        assert len(results) == 0

    def test_size_property(self):
        idx = self._make_index()
        assert idx.size == 0
        idx.add_document("d1", "first")
        assert idx.size == 1
        idx.add_document("d2", "second")
        assert idx.size == 2

    def test_batch_add_documents(self):
        idx = self._make_index()
        docs = [("d1", "hello world"), ("d2", "foo bar"), ("d3", "baz qux")]
        idx.batch_add_documents(docs)
        assert idx.size == 3

    def test_no_match_returns_empty(self):
        idx = self._make_index()
        idx.add_document("d1", "cats and dogs")
        results = idx.search("elephant")
        assert results == []

    def test_source_is_bm25(self):
        idx = self._make_index()
        idx.add_document("d1", "search engine optimization")
        results = idx.search("search")
        if results:
            assert results[0].source == "bm25"

    def test_tokenize_lowercases(self):
        from core.hybrid_retrieval import BM25Index
        tokens = BM25Index._tokenize("Hello WORLD FoO")
        assert all(t == t.lower() for t in tokens)

    def test_tokenize_extracts_words(self):
        from core.hybrid_retrieval import BM25Index
        tokens = BM25Index._tokenize("hello, world! foo-bar baz_qux")
        assert "hello" in tokens
        assert "world" in tokens


# ── RetrievalResult Dataclass Tests ──────────────────────────────

class TestRetrievalResult:
    """Test RetrievalResult dataclass."""

    def test_creation(self):
        from core.hybrid_retrieval import RetrievalResult
        r = RetrievalResult(id="t1", content="test", score=0.9, source="bm25")
        assert r.id == "t1"
        assert r.score == 0.9
        assert r.source == "bm25"
        assert r.metadata == {}
        assert r.rank == 0

    def test_metadata_default(self):
        from core.hybrid_retrieval import RetrievalResult
        r = RetrievalResult(id="t1", content="c", score=0.5, source="vector")
        assert isinstance(r.metadata, dict)


# ── FusedResult Dataclass Tests ──────────────────────────────────

class TestFusedResult:
    """Test FusedResult dataclass."""

    def test_creation(self):
        from core.hybrid_retrieval import FusedResult
        f = FusedResult(
            id="f1", content="fused", rrf_score=0.5,
            source_scores={"bm25": 0.8}, source_ranks={"bm25": 1}
        )
        assert f.id == "f1"
        assert f.rrf_score == 0.5
        assert f.source_scores == {"bm25": 0.8}

    def test_metadata_default(self):
        from core.hybrid_retrieval import FusedResult
        f = FusedResult(id="f1", content="c", rrf_score=0.1,
                        source_scores={}, source_ranks={})
        assert isinstance(f.metadata, dict)


# ── HybridRetriever Tests ────────────────────────────────────────

class TestHybridRetriever:
    """Test HybridRetriever with BM25 + optional vector/memory."""

    def _make_retriever(self, lance_store=None, memory=None):
        from core.hybrid_retrieval import HybridRetriever
        return HybridRetriever(lance_store=lance_store, memory=memory)

    def test_index_and_search_bm25_only(self):
        r = self._make_retriever()
        r.index_document("d1", "machine learning algorithms")
        r.index_document("d2", "deep learning neural networks")
        r.index_document("d3", "web development frameworks")
        results = r.search("learning", limit=5)
        assert len(results) >= 1
        # Results should include d1 and d2
        ids = [res.id for res in results]
        assert "d1" in ids or "d2" in ids

    def test_search_empty_query(self):
        r = self._make_retriever()
        r.index_document("d1", "some content")
        results = r.search("", limit=5)
        assert results == []

    def test_batch_index(self):
        r = self._make_retriever()
        docs = [("d1", "alpha"), ("d2", "beta"), ("d3", "gamma")]
        r.batch_index_documents(docs)
        results = r.search("alpha")
        assert len(results) >= 1

    def test_stats_property(self):
        r = self._make_retriever()
        r.index_document("d1", "test")
        stats = r.stats
        assert "bm25_documents" in stats
        assert stats["bm25_documents"] == 1
        assert stats["has_vector"] is False
        assert stats["has_memory"] is False

    def test_stats_with_lance(self):
        mock_lance = MagicMock()
        r = self._make_retriever(lance_store=mock_lance)
        stats = r.stats
        assert stats["has_vector"] is True

    def test_stats_with_memory(self):
        mock_mem = MagicMock()
        r = self._make_retriever(memory=mock_mem)
        stats = r.stats
        assert stats["has_memory"] is True

    def test_rrf_fusion_scores(self):
        """Test that RRF fusion produces meaningful combined scores."""
        r = self._make_retriever()
        r.index_document("d1", "python programming language")
        r.index_document("d2", "python scripting automation")
        results = r.search("python", limit=2)
        assert len(results) >= 1
        assert all(res.rrf_score > 0 for res in results)

    def test_rrf_fusion_sorted_desc(self):
        r = self._make_retriever()
        for i in range(5):
            r.index_document(f"d{i}", f"document {i} with search terms")
        results = r.search("search terms", limit=5)
        scores = [res.rrf_score for res in results]
        assert scores == sorted(scores, reverse=True)

    def test_vector_search_failure_graceful(self):
        """Vector search failure should not crash; falls back to BM25 only."""
        mock_lance = MagicMock()
        mock_lance.search_text.side_effect = Exception("connection failed")
        r = self._make_retriever(lance_store=mock_lance)
        r.index_document("d1", "fallback test document")
        results = r.search("fallback")
        assert len(results) >= 1  # BM25 results still returned

    def test_memory_search_failure_graceful(self):
        mock_mem = MagicMock()
        mock_mem.retrieve.side_effect = Exception("memory error")
        r = self._make_retriever(memory=mock_mem)
        r.index_document("d1", "memory test doc")
        results = r.search("memory")
        assert len(results) >= 1

    def test_limit_respected(self):
        r = self._make_retriever()
        for i in range(20):
            r.index_document(f"d{i}", f"common word document number {i}")
        results = r.search("common", limit=5)
        assert len(results) <= 5
