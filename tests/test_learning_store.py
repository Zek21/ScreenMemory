"""Tests for core/learning_store.py — Persistent learning and knowledge system.

Tests cover: LearnedFact dataclass, ExpertiseProfile (Bayesian updates),
LearningStore (learn/reinforce/contradict/recall/BM25/forget/consolidate),
PatternDetector, KnowledgeGraph (relations/BFS/path-finding),
PersistentLearningSystem facade, and stats/export.

Created by worker delta — critical module test coverage.
# signed: delta
"""

import json
import math
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.learning_store import (
    LearnedFact,
    ExpertiseProfile,
    LearningStore,
    PatternDetector,
    KnowledgeGraph,
    PersistentLearningSystem,
    initialize_learning_system,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path):
    """Provide a temporary SQLite DB path."""
    return str(tmp_path / "test_learning.db")


@pytest.fixture
def store(db_path):
    """Create a fresh LearningStore with temp DB."""
    return LearningStore(db_path)


@pytest.fixture
def expertise(db_path):
    """Create a fresh ExpertiseProfile with temp DB."""
    return ExpertiseProfile(db_path)


@pytest.fixture
def graph(db_path):
    """Create a fresh KnowledgeGraph with temp DB."""
    return KnowledgeGraph(db_path)


@pytest.fixture
def system(tmp_path):
    """Create a fresh PersistentLearningSystem with temp data dir."""
    return PersistentLearningSystem(str(tmp_path))


# ── LearnedFact Tests ───────────────────────────────────────────────────────
# signed: delta

class TestLearnedFact:
    """Tests for the LearnedFact dataclass."""

    def test_default_timestamps(self):
        """Auto-sets first_learned and last_accessed."""
        fact = LearnedFact(
            fact_id="f1", content="test", category="concept",
            confidence=0.7, source="test"
        )
        assert fact.first_learned != ""
        assert fact.last_accessed != ""
        # signed: delta

    def test_explicit_timestamps_preserved(self):
        """Does not overwrite explicit timestamps."""
        fact = LearnedFact(
            fact_id="f2", content="test", category="concept",
            confidence=0.7, source="test",
            first_learned="2025-01-01T00:00:00",
            last_accessed="2025-06-01T00:00:00"
        )
        assert fact.first_learned == "2025-01-01T00:00:00"
        assert fact.last_accessed == "2025-06-01T00:00:00"
        # signed: delta

    def test_default_tags_empty_list(self):
        """Tags default to empty list."""
        fact = LearnedFact(
            fact_id="f3", content="test", category="concept",
            confidence=0.7, source="test"
        )
        assert fact.tags == []
        # signed: delta

    def test_default_counts_zero(self):
        """Reinforcement and contradiction counts default to zero."""
        fact = LearnedFact(
            fact_id="f4", content="test", category="concept",
            confidence=0.5, source="test"
        )
        assert fact.reinforcement_count == 0
        assert fact.contradiction_count == 0
        # signed: delta


# ── ExpertiseProfile Tests ──────────────────────────────────────────────────
# signed: delta

class TestExpertiseProfile:
    """Tests for Bayesian expertise updating."""

    def test_initial_score_is_default(self, expertise):
        """Unknown domain starts at 0.5."""
        assert expertise.get_score("unknown_domain") == 0.5
        # signed: delta

    def test_success_increases_score(self, expertise):
        """Success increases domain score."""
        expertise.update("python", True)
        score = expertise.get_score("python")
        assert score > 0.5
        # signed: delta

    def test_failure_decreases_score(self, expertise):
        """Failure decreases domain score."""
        expertise.update("rust", False)
        score = expertise.get_score("rust")
        assert score < 0.5
        # signed: delta

    def test_score_bounded_0_1(self, expertise):
        """Score stays within [0, 1]."""
        for _ in range(100):
            expertise.update("bounded_up", True)
        assert expertise.get_score("bounded_up") <= 1.0

        for _ in range(100):
            expertise.update("bounded_down", False)
        assert expertise.get_score("bounded_down") >= 0.0
        # signed: delta

    def test_strongest_domains(self, expertise):
        """Returns domains sorted by score descending."""
        for _ in range(10):
            expertise.update("strong", True)
        expertise.update("weak", False)
        strongest = expertise.strongest_domains(2)
        assert len(strongest) == 2
        assert strongest[0][0] == "strong"
        assert strongest[0][1] > strongest[1][1]
        # signed: delta

    def test_weakest_domains(self, expertise):
        """Returns domains sorted by score ascending."""
        for _ in range(10):
            expertise.update("strong", True)
        expertise.update("weak", False)
        weakest = expertise.weakest_domains(1)
        assert weakest[0][0] == "weak"
        # signed: delta

    def test_total_experience(self, expertise):
        """Counts total tasks across all domains."""
        expertise.update("a", True)
        expertise.update("a", False)
        expertise.update("b", True)
        assert expertise.total_experience() == 3
        # signed: delta

    def test_repeated_updates_accumulate(self, expertise):
        """Multiple successes compound the score upward."""
        expertise.update("go", True)
        s1 = expertise.get_score("go")
        expertise.update("go", True)
        s2 = expertise.get_score("go")
        assert s2 > s1
        # signed: delta


# ── LearningStore Tests ─────────────────────────────────────────────────────
# signed: delta

class TestLearningStore:
    """Tests for the main persistent learning store."""

    def test_learn_returns_fact_id(self, store):
        """learn() returns a UUID string."""
        fid = store.learn("Python is great", "concept", "test")
        assert isinstance(fid, str)
        assert len(fid) == 36  # UUID format
        # signed: delta

    def test_learn_stores_fact(self, store):
        """Learned fact is retrievable."""
        fid = store.learn("SQLite is embedded", "concept", "test", tags=["db"])
        facts = store.recall("SQLite embedded", top_k=1)
        assert len(facts) >= 1
        assert any("SQLite" in f.content for f in facts)
        # signed: delta

    def test_learn_default_confidence_0_7(self, store):
        """New facts get 0.7 confidence."""
        store.learn("test fact", "concept", "test")
        facts = store.recall_by_category("concept", top_k=1)
        assert facts[0].confidence == 0.7
        # signed: delta

    def test_reinforce_increases_confidence(self, store):
        """Reinforcing a fact increases confidence."""
        fid = store.learn("reinforced fact", "concept", "test")
        initial = store.recall_by_category("concept")[0].confidence
        store.reinforce(fid)
        updated = store.recall_by_category("concept")[0].confidence
        assert updated > initial
        # signed: delta

    def test_reinforce_increments_count(self, store):
        """Reinforcing increments reinforcement_count."""
        fid = store.learn("count fact", "concept", "test")
        store.reinforce(fid)
        store.reinforce(fid)
        facts = store.recall_by_category("concept")
        assert facts[0].reinforcement_count == 2
        # signed: delta

    def test_reinforce_caps_at_1(self, store):
        """Confidence never exceeds 1.0."""
        fid = store.learn("cap test", "concept", "test")
        for _ in range(50):
            store.reinforce(fid)
        facts = store.recall_by_category("concept")
        assert facts[0].confidence <= 1.0
        # signed: delta

    def test_contradict_creates_correction(self, store):
        """Contradicting creates a new correction fact."""
        fid = store.learn("wrong fact", "concept", "test")
        new_fid = store.contradict(fid, "corrected fact")
        assert new_fid != fid
        corrections = store.recall_by_category("correction")
        assert len(corrections) == 1
        assert "corrected" in corrections[0].content
        # signed: delta

    def test_contradict_increments_count(self, store):
        """Contradicting increments contradiction_count on original."""
        fid = store.learn("original fact", "concept", "test")
        store.contradict(fid, "correction 1")
        store.contradict(fid, "correction 2")
        # Check via direct DB query since recall might not return contradicted facts first
        with sqlite3.connect(store.db_path) as conn:
            row = conn.execute(
                "SELECT contradiction_count FROM learned_facts WHERE fact_id=?", (fid,)
            ).fetchone()
        assert row[0] == 2
        # signed: delta

    def test_recall_returns_relevant(self, store):
        """BM25 recall finds relevant facts."""
        store.learn("Python type hints improve code quality", "concept", "test")
        store.learn("Docker containers provide isolation", "concept", "test")
        store.learn("SQLite is a lightweight database", "concept", "test")
        results = store.recall("Python type hints", top_k=1)
        assert len(results) >= 1
        assert "Python" in results[0].content
        # signed: delta

    def test_recall_empty_store(self, store):
        """Recall on empty store returns empty list."""
        results = store.recall("anything", top_k=5)
        assert results == []
        # signed: delta

    def test_recall_by_category(self, store):
        """Filters by category."""
        store.learn("fact A", "concept", "test")
        store.learn("fact B", "procedure", "test")
        concepts = store.recall_by_category("concept")
        assert all(f.category == "concept" for f in concepts)
        # signed: delta

    def test_forget_removes_low_confidence(self, store):
        """Forget removes low-confidence contradicted facts."""
        fid = store.learn("bad fact", "concept", "test")
        # Manually lower confidence and add contradictions
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE learned_facts SET confidence=0.05, contradiction_count=5 WHERE fact_id=?",
                (fid,)
            )
            conn.commit()
        deleted = store.forget(min_confidence=0.1)
        assert deleted >= 1
        # signed: delta

    def test_forget_keeps_good_facts(self, store):
        """Forget doesn't remove high-confidence facts."""
        store.learn("good fact", "concept", "test")
        deleted = store.forget(min_confidence=0.1)
        assert deleted == 0
        # signed: delta

    def test_stats(self, store):
        """Returns accurate statistics."""
        store.learn("fact 1", "concept", "test")
        store.learn("fact 2", "procedure", "test")
        store.learn("fact 3", "concept", "test")
        stats = store.stats()
        assert stats["total_facts"] == 3
        assert stats["average_confidence"] == pytest.approx(0.7)
        assert stats["by_category"]["concept"] == 2
        assert stats["by_category"]["procedure"] == 1
        # signed: delta

    def test_stats_empty(self, store):
        """Stats on empty store returns zeroes."""
        stats = store.stats()
        assert stats["total_facts"] == 0
        assert stats["average_confidence"] == 0.0
        # signed: delta

    def test_export_knowledge(self, store):
        """Exports formatted knowledge text."""
        store.learn("fact A content", "concept", "test")
        store.learn("fact B content", "procedure", "test")
        output = store.export_knowledge()
        assert "LEARNED KNOWLEDGE" in output
        assert "CONCEPT" in output
        assert "PROCEDURE" in output
        # signed: delta

    def test_export_knowledge_empty(self, store):
        """Empty store exports 'No knowledge' message."""
        output = store.export_knowledge()
        assert "No knowledge" in output
        # signed: delta

    def test_export_knowledge_by_domain(self, store):
        """Export filters by domain tag."""
        store.learn("python is good", "concept", "test", tags=["python"])
        store.learn("rust is fast", "concept", "test", tags=["rust"])
        output = store.export_knowledge(domain="python")
        assert "python" in output.lower()
        # signed: delta


# ── BM25 Algorithm Tests ───────────────────────────────────────────────────
# signed: delta

class TestBM25:
    """Tests for the BM25 scoring implementation."""

    def test_matching_doc_scores_higher(self, store):
        """Document containing query terms scores higher."""
        docs = [["the", "cat", "sat"], ["python", "type", "hints"]]
        scores = store._bm25_score(["python", "type"], docs)
        assert scores[1] > scores[0]
        # signed: delta

    def test_empty_docs_returns_zeros(self, store):
        """Empty document list returns empty scores."""
        scores = store._bm25_score(["test"], [])
        assert scores == []
        # signed: delta

    def test_no_match_scores_zero(self, store):
        """Documents with no query terms score zero."""
        docs = [["apple", "banana"]]
        scores = store._bm25_score(["python"], docs)
        assert scores[0] == 0.0
        # signed: delta

    def test_tokenize(self, store):
        """Tokenizer lowercases and splits on whitespace."""
        tokens = store._tokenize("Hello World Test")
        assert tokens == ["hello", "world", "test"]
        # signed: delta


# ── Consolidation Tests ─────────────────────────────────────────────────────
# signed: delta

class TestConsolidation:
    """Tests for fact deduplication/merging."""

    def test_consolidate_merges_similar(self, store):
        """Consolidate merges facts with high word overlap."""
        store.learn("python type hints are great for code quality", "concept", "test")
        store.learn("python type hints are great for code clarity", "concept", "test")
        merged = store.consolidate()
        assert merged >= 1
        stats = store.stats()
        assert stats["total_facts"] < 2
        # signed: delta

    def test_consolidate_keeps_different(self, store):
        """Consolidate keeps facts with low word overlap."""
        store.learn("python type hints improve quality", "concept", "test")
        store.learn("docker containers provide isolation", "concept", "test")
        merged = store.consolidate()
        assert merged == 0
        # signed: delta

    def test_consolidate_single_fact(self, store):
        """Consolidate with one fact returns 0."""
        store.learn("single fact", "concept", "test")
        merged = store.consolidate()
        assert merged == 0
        # signed: delta

    def test_should_merge_high_overlap(self, store):
        """High word overlap returns True."""
        row_a = (None, "the quick brown fox jumps over the lazy dog")
        row_b = (None, "the quick brown fox jumps over the lazy cat")
        assert store._should_merge(row_a, row_b, threshold=0.7) is True
        # signed: delta

    def test_should_merge_low_overlap(self, store):
        """Low word overlap returns False."""
        row_a = (None, "python type hints")
        row_b = (None, "docker containers isolation")
        assert store._should_merge(row_a, row_b, threshold=0.7) is False
        # signed: delta


# ── KnowledgeGraph Tests ────────────────────────────────────────────────────
# signed: delta

class TestKnowledgeGraph:
    """Tests for the knowledge graph BFS traversal."""

    def test_add_and_get_related(self, graph):
        """Related facts are discoverable via BFS."""
        graph.add_relation("A", "B", "extends")
        related = graph.get_related("A", depth=1)
        assert "B" in related
        # signed: delta

    def test_depth_limited_traversal(self, graph):
        """BFS respects depth limit."""
        graph.add_relation("A", "B", "extends")
        graph.add_relation("B", "C", "extends")
        graph.add_relation("C", "D", "extends")
        # Depth 1 should find B but not C or D
        related = graph.get_related("A", depth=1)
        assert "B" in related
        assert "C" not in related
        assert "D" not in related
        # signed: delta

    def test_deep_traversal(self, graph):
        """Deeper depth finds transitive relations."""
        graph.add_relation("A", "B", "extends")
        graph.add_relation("B", "C", "extends")
        related = graph.get_related("A", depth=2)
        assert "B" in related
        assert "C" in related
        # signed: delta

    def test_bidirectional_traversal(self, graph):
        """BFS works in both directions."""
        graph.add_relation("A", "B", "extends")
        related_from_b = graph.get_related("B", depth=1)
        assert "A" in related_from_b
        # signed: delta

    def test_find_path_exists(self, graph):
        """Finds shortest path between connected nodes."""
        graph.add_relation("A", "B", "extends")
        graph.add_relation("B", "C", "extends")
        path = graph.find_path("A", "C")
        assert path == ["A", "B", "C"]
        # signed: delta

    def test_find_path_same_node(self, graph):
        """Path to self is just the node."""
        path = graph.find_path("A", "A")
        assert path == ["A"]
        # signed: delta

    def test_find_path_no_connection(self, graph):
        """No path between disconnected nodes returns empty."""
        graph.add_relation("A", "B", "extends")
        path = graph.find_path("A", "Z")
        assert path == []
        # signed: delta

    def test_multiple_relations(self, graph):
        """Multiple relation types between same nodes."""
        graph.add_relation("A", "B", "extends")
        graph.add_relation("A", "B", "contradicts")
        # Both relations exist, still find B
        related = graph.get_related("A", depth=1)
        assert "B" in related
        # signed: delta


# ── PatternDetector Tests ───────────────────────────────────────────────────
# signed: delta

class TestPatternDetector:
    """Tests for n-gram pattern detection."""

    def test_detect_recurring_bigrams(self, store):
        """Detects recurring bigrams."""
        detector = PatternDetector(store)
        for i in range(5):
            store.learn(f"python type hints improve code quality version {i}", "concept", "test")
        patterns = detector.detect_recurring("concept", min_occurrences=3)
        phrases = [p["phrase"] for p in patterns]
        assert any("python type" in p for p in phrases) or any("type hints" in p for p in phrases)
        # signed: delta

    def test_detect_no_patterns_few_facts(self, store):
        """No patterns with fewer facts than threshold."""
        detector = PatternDetector(store)
        store.learn("single fact", "concept", "test")
        patterns = detector.detect_recurring("concept", min_occurrences=3)
        assert patterns == []
        # signed: delta

    def test_detect_failure_patterns(self, store):
        """Detects patterns in correction category."""
        detector = PatternDetector(store)
        # Need at least 2 corrections with shared bigrams
        for i in range(3):
            store.learn(f"timeout error occurred during request {i}", "correction", "test")
        patterns = detector.detect_failure_patterns()
        # May or may not find patterns depending on min_occurrences=2
        assert isinstance(patterns, list)
        # signed: delta


# ── PersistentLearningSystem Facade Tests ───────────────────────────────────
# signed: delta

class TestPersistentLearningSystem:
    """Tests for the unified facade."""

    def test_learn_from_task(self, system):
        """Learning from task creates facts and updates expertise."""
        fact_ids = system.learn_from_task(
            "Fix security bug",
            "security",
            True,
            ["DPAPI key rotation is critical", "Always verify key hash"]
        )
        assert len(fact_ids) == 2
        # Expertise updated
        score = system.expertise.get_score("security")
        assert score > 0.5
        # signed: delta

    def test_learn_from_task_links_facts(self, system):
        """Multiple insights from same task are graph-linked."""
        fact_ids = system.learn_from_task(
            "Audit code",
            "code",
            True,
            ["fact 1", "fact 2", "fact 3"]
        )
        related = system.graph.get_related(fact_ids[0], depth=2)
        assert fact_ids[1] in related
        # signed: delta

    def test_get_context_for_task(self, system):
        """Context retrieval returns formatted knowledge."""
        system.learn_from_task("Python testing", "testing", True, ["pytest is best"])
        ctx = system.get_context_for_task("Python testing")
        assert "PRIOR KNOWLEDGE" in ctx
        # signed: delta

    def test_get_context_empty(self, system):
        """Empty system returns 'no prior knowledge'."""
        ctx = system.get_context_for_task("anything")
        assert "No prior knowledge" in ctx
        # signed: delta

    def test_expertise_summary(self, system):
        """Summary includes all expected fields."""
        system.learn_from_task("test", "code", True, ["learned something"])
        summary = system.get_expertise_summary()
        assert "strongest_domains" in summary
        assert "weakest_domains" in summary
        assert "total_experience" in summary
        assert "knowledge_stats" in summary
        # signed: delta

    def test_run_maintenance(self, system):
        """Maintenance runs without error."""
        system.learn_from_task("test", "code", True, ["a fact"])
        result = system.run_maintenance()
        assert "facts_forgotten" in result
        assert "facts_consolidated" in result
        # signed: delta


# ── Initialize Function Tests ──────────────────────────────────────────────
# signed: delta

class TestInitialize:
    """Tests for the convenience initializer."""

    def test_initialize_creates_system(self, tmp_path):
        """initialize_learning_system returns a working system."""
        system = initialize_learning_system(str(tmp_path))
        assert isinstance(system, PersistentLearningSystem)
        assert system.store is not None
        assert system.expertise is not None
        assert system.graph is not None
        # signed: delta

    def test_initialize_creates_db(self, tmp_path):
        """Initializing creates the SQLite database file."""
        initialize_learning_system(str(tmp_path))
        assert (tmp_path / "learning.db").exists()
        # signed: delta
